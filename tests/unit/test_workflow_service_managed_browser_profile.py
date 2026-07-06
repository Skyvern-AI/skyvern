from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.browser_profile_key import (
    build_browser_profile_key_digest,
    build_workflow_browser_session_storage_key,
)
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.forge.sdk.workflow.service import WorkflowService


def _workflow(browser_profile_key: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        persist_browser_session=True,
        browser_profile_key=browser_profile_key,
        workflow_permanent_id="wpid_test",
        title="Workflow",
    )


def _workflow_run(browser_profile_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_profile_id=browser_profile_id,
    )


def _mock_storage(monkeypatch: pytest.MonkeyPatch, *, legacy_dir: str | None) -> AsyncMock:
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", AsyncMock(return_value=legacy_dir))
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    return store


@pytest.mark.asyncio
async def test_prepare_persisted_workflow_browser_profile_stamps_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    updated_run = _workflow_run(browser_profile_id="bp_managed")
    get_or_create = AsyncMock(return_value=(profile, True))
    update_run = AsyncMock(return_value=updated_run)
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update_run)
    _mock_storage(monkeypatch, legacy_dir=None)

    result = await WorkflowService()._prepare_persisted_workflow_browser_profile(
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        workflow_run=_workflow_run(),
        parameter_values={"credential_id": "cred_123"},
    )

    assert result is updated_run
    get_or_create.assert_awaited_once_with(
        organization_id="o_test",
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest=build_browser_profile_key_digest("cred_123"),
        name="Workflow (auto-saved: cred_123)",
    )
    update_run.assert_awaited_once_with(workflow_run_id="wr_test", browser_profile_id="bp_managed")


@pytest.mark.asyncio
async def test_prepare_persisted_workflow_browser_profile_uses_empty_digest_for_unkeyed_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    updated_run = _workflow_run(browser_profile_id="bp_managed")
    get_or_create = AsyncMock(return_value=(profile, True))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", AsyncMock(return_value=updated_run))
    _mock_storage(monkeypatch, legacy_dir=None)

    await WorkflowService()._prepare_persisted_workflow_browser_profile(
        workflow=_workflow(),
        workflow_run=_workflow_run(),
        parameter_values={},
    )

    get_or_create.assert_awaited_once_with(
        organization_id="o_test",
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="",
        name="Workflow (auto-saved session)",
    )


@pytest.mark.asyncio
async def test_prepare_persisted_workflow_browser_profile_keeps_explicit_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_or_create = AsyncMock()
    update_run = AsyncMock()
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update_run)
    workflow_run = _workflow_run(browser_profile_id="bp_user")

    result = await WorkflowService()._prepare_persisted_workflow_browser_profile(
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        workflow_run=workflow_run,
        parameter_values={"credential_id": "cred_123"},
    )

    assert result is workflow_run
    get_or_create.assert_not_awaited()
    update_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_persisted_workflow_browser_profile_seeds_new_profile_from_legacy_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    get_or_create = AsyncMock(return_value=(profile, True))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs, "update_workflow_run", AsyncMock(return_value=_workflow_run("bp_managed"))
    )
    store = _mock_storage(monkeypatch, legacy_dir="/tmp/legacy_session")

    await WorkflowService()._prepare_persisted_workflow_browser_profile(
        workflow=_workflow(),
        workflow_run=_workflow_run(),
        parameter_values={},
    )

    store.assert_awaited_once_with(
        "o_test",
        profile_id="bp_managed",
        directory="/tmp/legacy_session",
    )


@pytest.mark.asyncio
async def test_prepare_persisted_workflow_browser_profile_rolls_back_on_seed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_or_create_managed_browser_profile",
        AsyncMock(return_value=(profile, True)),
    )
    hard_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE.browser_sessions, "hard_delete_browser_profile", hard_delete)
    update_run = AsyncMock()
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update_run)
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", AsyncMock(return_value="/tmp/legacy_session"))
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", AsyncMock(side_effect=RuntimeError("upload failed")))
    workflow_run = _workflow_run()

    result = await WorkflowService()._prepare_persisted_workflow_browser_profile(
        workflow=_workflow(),
        workflow_run=workflow_run,
        parameter_values={},
    )

    assert result is workflow_run
    hard_delete.assert_awaited_once_with(profile_id="bp_managed", organization_id="o_test")
    update_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_persisted_workflow_browser_profile_does_not_seed_existing_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    get_or_create = AsyncMock(return_value=(profile, False))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs, "update_workflow_run", AsyncMock(return_value=_workflow_run("bp_managed"))
    )
    retrieve = AsyncMock(return_value="/tmp/legacy_session")
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", retrieve)
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)

    await WorkflowService()._prepare_persisted_workflow_browser_profile(
        workflow=_workflow(),
        workflow_run=_workflow_run(),
        parameter_values={},
    )

    retrieve.assert_not_awaited()
    store.assert_not_awaited()


def test_legacy_storage_key_matches_managed_digest_for_keyed_workflow() -> None:
    # Read-compat invariant: the managed profile digest and the legacy archive segment must
    # derive from the same rendered key, so seeding finds the right archive.
    rendered = "cred_123"
    storage_key = build_workflow_browser_session_storage_key("wpid_test", rendered)
    assert storage_key.endswith(build_browser_profile_key_digest(rendered))


@pytest.mark.asyncio
async def test_auto_create_browser_session_for_human_interaction_loads_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_session = AsyncMock(return_value=SimpleNamespace(persistent_browser_session_id="pbs_test"))
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "create_session", create_session)
    workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[SimpleNamespace(block_type=BlockType.HUMAN_INTERACTION, timeout_seconds=60)]
        )
    )

    browser_session = await WorkflowService().auto_create_browser_session_if_needed(
        "o_test",
        workflow,
        browser_profile_id="bp_managed",
    )

    assert browser_session.persistent_browser_session_id == "pbs_test"
    create_session.assert_awaited_once_with(
        organization_id="o_test",
        timeout_minutes=61,
        browser_profile_id="bp_managed",
        proxy_location=None,
    )


@pytest.mark.asyncio
async def test_browser_profile_is_managed_distinguishes_user_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    get_browser_profile = AsyncMock(return_value=SimpleNamespace(is_managed=False))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_browser_profile", get_browser_profile)

    is_managed = await WorkflowService()._browser_profile_is_managed(
        organization_id="o_test",
        browser_profile_id="bp_user",
    )

    assert is_managed is False
    get_browser_profile.assert_awaited_once_with(profile_id="bp_user", organization_id="o_test")


@pytest.mark.asyncio
async def test_browser_profile_is_managed_detects_managed_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    get_browser_profile = AsyncMock(return_value=SimpleNamespace(is_managed=True))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_browser_profile", get_browser_profile)

    is_managed = await WorkflowService()._browser_profile_is_managed(
        organization_id="o_test",
        browser_profile_id="bp_managed",
    )

    assert is_managed is True
