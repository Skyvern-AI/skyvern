from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.browser_profile_key import (
    build_browser_profile_key_digest,
    build_workflow_browser_session_storage_key,
)
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import (
    CODE_BLOCK_SESSION_TIMEOUT_MINUTES,
    WorkflowBrowserCleanupResult,
    WorkflowService,
)


def _workflow(browser_profile_key: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        persist_browser_session=True,
        pin_saved_session_ip=False,
        browser_profile_key=browser_profile_key,
        workflow_permanent_id="wpid_test",
        title="Workflow",
    )


def _workflow_run(browser_profile_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_profile_id=browser_profile_id,
        proxy_location=None,
    )


def _execute_workflow() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=True,
        workflow_permanent_id="wpid_test",
        title="Workflow",
        organization_id="o_test",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(parameters=[], finally_block_label=None, blocks=[]),
    )


def _execute_workflow_run(status: WorkflowRunStatus) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        workflow_run_id="wr_test",
        workflow_id="wf_1",
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        browser_profile_id="bp_managed",
        browser_address=None,
        status=status,
        failure_reason=None,
        ignore_inherited_workflow_system_prompt=False,
        parent_workflow_run_id=None,
        proxy_location=None,
        max_elapsed_time_minutes=1,
        started_at=now,
        created_at=now,
        code_gen=False,
        run_with="agent",
    )


def _browser_cleanup_result() -> WorkflowBrowserCleanupResult:
    browser_state = SimpleNamespace(
        browser_artifacts=SimpleNamespace(browser_session_dir="/tmp/fake_profile"),
        browser_context=SimpleNamespace(),
    )
    return WorkflowBrowserCleanupResult(
        browser_state=browser_state,
        tasks=[],
        all_workflow_task_ids=[],
        child_workflow_run_ids=[],
        close_browser_on_completion=True,
    )


def _patch_execute_workflow_deps(
    monkeypatch: pytest.MonkeyPatch,
    svc: WorkflowService,
    workflow: SimpleNamespace,
    refreshed_run: SimpleNamespace,
) -> None:
    created_run = _execute_workflow_run(WorkflowRunStatus.created)
    running_run = _execute_workflow_run(WorkflowRunStatus.running)
    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
        remove_workflow_run_context=lambda _workflow_run_id: None,
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=refreshed_run),
            update_workflow_run=AsyncMock(),
        ),
        artifacts=SimpleNamespace(claim_session_download_artifacts_for_run=AsyncMock(return_value=0)),
    )

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_for_code_block_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(return_value=(refreshed_run, set())))
    monkeypatch.setattr(svc, "generate_script_if_needed", AsyncMock())
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))


def _patch_browser_cleanup(monkeypatch: pytest.MonkeyPatch, svc: WorkflowService, order: list[str]) -> AsyncMock:
    clean_up_browser = AsyncMock(side_effect=lambda **_kwargs: order.append("teardown") or _browser_cleanup_result())
    monkeypatch.setattr(svc, "_clean_up_workflow_browser", clean_up_browser)
    return clean_up_browser


def _patch_finalize(
    monkeypatch: pytest.MonkeyPatch,
    svc: WorkflowService,
    order: list[str],
    finalized_run: SimpleNamespace,
) -> None:
    monkeypatch.setattr(
        svc,
        "_finalize_workflow_run_status",
        AsyncMock(side_effect=lambda **_kwargs: order.append("finalize") or finalized_run),
    )


async def _run_execute_workflow(svc: WorkflowService) -> SimpleNamespace:
    return await svc.execute_workflow(
        workflow_run_id="wr_test",
        api_key=None,
        organization=SimpleNamespace(organization_id="o_test"),
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
        inherit_profile_proxy=True,
    )


@pytest.mark.asyncio
async def test_auto_create_browser_session_for_code_block_loads_managed_profile_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_session = AsyncMock(return_value=SimpleNamespace(persistent_browser_session_id="pbs_test"))
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "create_session", create_session)
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_auto_create_browser_session_for_code_block",
        AsyncMock(return_value=True),
    )
    workflow = SimpleNamespace(
        workflow_id="wf_1",
        workflow_permanent_id="wpid_test",
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(block_type=BlockType.CODE, loop_blocks=[])]),
    )

    browser_session = await WorkflowService().auto_create_browser_session_for_code_block_if_needed(
        "o_test",
        workflow,
        workflow_run_id="wr_test",
        browser_profile_id="bp_managed",
    )

    assert browser_session.persistent_browser_session_id == "pbs_test"
    create_session.assert_awaited_once_with(
        organization_id="o_test",
        timeout_minutes=CODE_BLOCK_SESSION_TIMEOUT_MINUTES,
        browser_profile_id="bp_managed",
        proxy_location=None,
        inherit_profile_proxy=True,
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


@pytest.mark.asyncio
async def test_execute_workflow_persists_managed_profile_before_final_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    clean_up_browser = _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(
        svc,
        "_persist_workflow_browser_session_if_needed",
        AsyncMock(side_effect=lambda **_kwargs: order.append("store")),
    )
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock(side_effect=lambda *a, **k: order.append("webhook")))

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order == ["teardown", "store", "finalize", "video", "webhook"]
    clean_up_browser.assert_awaited_once()
    svc._persist_workflow_browser_session_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_retries_write_back_when_pre_final_persist_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []
    persist_calls = {"n": 0}

    async def persist_side_effect(**_kwargs: object) -> None:
        persist_calls["n"] += 1
        if persist_calls["n"] == 1:
            order.append("store_fail")
            raise RuntimeError("storage down")
        order.append("store_retry")

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", AsyncMock(side_effect=persist_side_effect))
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock(side_effect=lambda *a, **k: order.append("webhook")))

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    # A failed pre-final write-back must not suppress clean_up_workflow's retry after final status.
    assert order == ["teardown", "store_fail", "finalize", "video", "store_retry", "webhook"]
    assert svc._persist_workflow_browser_session_if_needed.await_count == 2


@pytest.mark.asyncio
async def test_execute_workflow_does_not_prestore_blob_for_failed_canceled_or_timed_out_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for status in (WorkflowRunStatus.failed, WorkflowRunStatus.canceled, WorkflowRunStatus.timed_out):
        workflow = _execute_workflow()
        terminal_run = _execute_workflow_run(status)
        order: list[str] = []

        svc = WorkflowService()
        _patch_execute_workflow_deps(monkeypatch, svc, workflow, terminal_run)
        _patch_browser_cleanup(monkeypatch, svc, order)
        monkeypatch.setattr(
            service_module.app.STORAGE,
            "store_browser_profile",
            AsyncMock(side_effect=lambda *a, **k: order.append("store_profile")),
        )
        monkeypatch.setattr(service_module.app.STORAGE, "store_browser_session", AsyncMock())
        _patch_finalize(monkeypatch, svc, order, terminal_run)
        monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
        monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())

        result = await _run_execute_workflow(svc)

        assert result is terminal_run
        assert order[:2] == ["finalize", "teardown"]
        assert "store_profile" not in order


@pytest.mark.asyncio
async def test_execute_workflow_non_persist_workflow_tears_down_once_after_final_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    workflow.persist_browser_session = False
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    clean_up_browser = _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(service_module.app.STORAGE, "store_browser_profile", AsyncMock())
    monkeypatch.setattr(service_module.app.STORAGE, "store_browser_session", AsyncMock())
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order[:2] == ["finalize", "teardown"]
    clean_up_browser.assert_awaited_once()
    service_module.app.STORAGE.store_browser_profile.assert_not_awaited()
    service_module.app.STORAGE.store_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_finalizes_when_pre_status_browser_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    def cleanup_side_effect(**_kwargs: object) -> WorkflowBrowserCleanupResult:
        if "teardown_error" not in order:
            order.append("teardown_error")
            raise RuntimeError("cleanup failed")
        order.append("teardown_cleanup")
        return _browser_cleanup_result()

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    monkeypatch.setattr(svc, "_clean_up_workflow_browser", AsyncMock(side_effect=cleanup_side_effect))
    monkeypatch.setattr(
        svc,
        "_persist_workflow_browser_session_if_needed",
        AsyncMock(side_effect=lambda **_kwargs: order.append("persist_helper")),
    )
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order[:2] == ["teardown_error", "finalize"]
