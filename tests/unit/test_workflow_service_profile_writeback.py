"""Tests for persistent browser profile write-back gating.

The persistent browser session should only be written back to S3 when the
workflow run completes successfully.  Crashed or failed runs must NOT
overwrite the shared S3 profile with their dirty state.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.browser_profile_key import build_workflow_browser_session_storage_key
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye.browser_manager import BrowserCleanupResult
from skyvern.webeye.profile_cookie_merge import BANKED_COOKIES_FILENAME


def _make_workflow(persist: bool = True) -> MagicMock:
    wf = MagicMock()
    wf.persist_browser_session = persist
    wf.workflow_permanent_id = "wpid_test"
    wf.browser_profile_key = None
    return wf


def _make_workflow_run(
    status: WorkflowRunStatus,
    browser_profile_id: str | None = None,
    browser_sink_profile_id: str | None = None,
    start_fresh_browser: bool | None = None,
) -> MagicMock:
    wr = MagicMock()
    wr.workflow_run_id = "wr_test"
    wr.organization_id = "o_test"
    wr.status = status
    wr.browser_profile_id = browser_profile_id
    wr.browser_sink_profile_id = browser_sink_profile_id
    wr.start_fresh_browser = start_fresh_browser
    wr.browser_seed_source = None
    wr.debug_session_id = None
    wr.browser_address = None
    wr.webhook_callback_url = None
    wr.created_at = None
    wr.workflow_permanent_id = "wpid_test"
    return wr


def _make_browser_state(applied_browser_profile_id: str | None = None) -> MagicMock:
    bs = MagicMock()
    bs.browser_artifacts.browser_session_dir = "/tmp/fake_profile"
    bs.browser_artifacts._seed_load_failed = False
    bs.browser_artifacts._seed_capture_failed = False
    bs.browser_artifacts.applied_browser_profile_id = applied_browser_profile_id
    return bs


def _patch_clean_up_deps(monkeypatch: pytest.MonkeyPatch, browser_state: MagicMock) -> AsyncMock:
    """Patch all external dependencies of clean_up_workflow. Returns the store mock."""
    store_mock = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_session", store_mock)
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", AsyncMock())
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(
        app.BROWSER_MANAGER,
        "cleanup_for_workflow_run",
        AsyncMock(
            side_effect=lambda *args, **kwargs: BrowserCleanupResult(
                browser_state=browser_state,
                recording_finalized=kwargs["close_browser_on_completion"],
            )
        ),
    )
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    # Non-debug default: the legacy write-back proceeds (debug-skip tests override this to True).
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_skip_debug_profile_writeback", AsyncMock(return_value=False))
    # Engine off by default: the legacy own-memory write-back path runs byte-for-byte (flag-on tests
    # override this to True to exercise the sink-driven path).
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[]),
    )
    return store_mock


@pytest.mark.asyncio
async def test_cleanup_reports_actual_recording_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed)
    browser_state = _make_browser_state()
    cleanup_mock = AsyncMock(return_value=BrowserCleanupResult(browser_state=browser_state, recording_finalized=False))
    monkeypatch.setattr(app.BROWSER_MANAGER, "cleanup_for_workflow_run", cleanup_mock)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[]),
    )

    service = WorkflowService()
    monkeypatch.setattr(service, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    result = await service._clean_up_workflow_browser(workflow_run)

    assert cleanup_mock.await_args.kwargs["close_browser_on_completion"] is True
    assert result.close_browser_on_completion is False


@pytest.mark.asyncio
async def test_materialize_own_profile_pick_on_first_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # B3: first successful engine run of a persist-ON no-pick own-memory workflow materializes the pick.
    from skyvern.forge.sdk.db.enums import BrowserSeedSource
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow.browser_profile_id = None
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp_own")
    wr.browser_seed_source = BrowserSeedSource.own_memory
    link = AsyncMock(return_value=True)
    monkeypatch.setattr(app.DATABASE.workflows, "link_workflow_browser_profile_if_unset", link)

    await WorkflowService()._materialize_own_profile_pick_if_needed(
        workflow=workflow, workflow_run=wr, effective_workflow_run_status=WorkflowRunStatus.completed
    )

    link.assert_awaited_once_with(
        workflow_permanent_id="wpid_test", organization_id="o_test", browser_profile_id="bp_own"
    )


@pytest.mark.asyncio
async def test_materialize_skips_when_already_picked_or_not_own_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.enums import BrowserSeedSource
    from skyvern.forge.sdk.workflow.service import WorkflowService

    link = AsyncMock()
    monkeypatch.setattr(app.DATABASE.workflows, "link_workflow_browser_profile_if_unset", link)
    svc = WorkflowService()

    # already has a pick → no materialize
    wf_picked = _make_workflow(persist=True)
    wf_picked.browser_profile_id = "bp_existing"
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp_own")
    wr.browser_seed_source = BrowserSeedSource.own_memory
    await svc._materialize_own_profile_pick_if_needed(
        workflow=wf_picked, workflow_run=wr, effective_workflow_run_status=WorkflowRunStatus.completed
    )

    # a pick seed (not own-memory) → no materialize
    wf_nopick = _make_workflow(persist=True)
    wf_nopick.browser_profile_id = None
    wr2 = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp_pick")
    wr2.browser_seed_source = BrowserSeedSource.picked
    await svc._materialize_own_profile_pick_if_needed(
        workflow=wf_nopick, workflow_run=wr2, effective_workflow_run_status=WorkflowRunStatus.completed
    )

    link.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_session_persisted_on_completed_run_without_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed)
    browser_state = _make_browser_state()
    store_mock = _patch_clean_up_deps(monkeypatch, browser_state)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    store_mock.assert_awaited_once_with("o_test", "wpid_test", "/tmp/fake_profile")
    app.STORAGE.store_browser_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_profile_persisted_on_completed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed, browser_profile_id="bp_managed")
    browser_state = _make_browser_state()
    store_session_mock = _patch_clean_up_deps(monkeypatch, browser_state)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(
            return_value=SimpleNamespace(
                is_managed=True, browser_profile_id="bp_managed", workflow_permanent_id="wpid_test"
            )
        ),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    app.STORAGE.store_browser_profile.assert_awaited_once_with(
        "o_test",
        profile_id="bp_managed",
        directory="/tmp/fake_profile",
    )
    store_session_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_debug_session_skips_legacy_writeback_when_engine_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # A debug (Studio) play of a Remember workflow must not overwrite known-good memory through the
    # legacy seam once the browser-memory engine is on for the org.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed, browser_profile_id="bp_managed")
    workflow_run.debug_session_id = "ds_1"
    browser_state = _make_browser_state()
    store_session_mock = _patch_clean_up_deps(monkeypatch, browser_state)
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_skip_debug_profile_writeback", AsyncMock(return_value=True))
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(
            return_value=SimpleNamespace(
                is_managed=True, browser_profile_id="bp_managed", workflow_permanent_id="wpid_test"
            )
        ),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    app.STORAGE.store_browser_profile.assert_not_awaited()
    store_session_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_managed_profile_not_persisted_on_completed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed profile owned by another workflow must not receive this run's write-back."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed, browser_profile_id="bp_foreign")
    browser_state = _make_browser_state()
    store_session_mock = _patch_clean_up_deps(monkeypatch, browser_state)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(
            return_value=SimpleNamespace(
                is_managed=True, browser_profile_id="bp_foreign", workflow_permanent_id="wpid_other"
            )
        ),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    app.STORAGE.store_browser_profile.assert_not_awaited()
    store_session_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_managed_profile_falls_back_to_legacy_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed profile stamped at setup but deleted before finalization must recover, not drop state."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed, browser_profile_id="bp_gone")
    browser_state = _make_browser_state()
    store_mock = _patch_clean_up_deps(monkeypatch, browser_state)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=None),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    store_mock.assert_awaited_once_with("o_test", "wpid_test", "/tmp/fake_profile")
    app.STORAGE.store_browser_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_profile_not_persisted_on_completed_persist_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed, browser_profile_id="bp_user")
    browser_state = _make_browser_state()
    store_session_mock = _patch_clean_up_deps(monkeypatch, browser_state)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False)),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    app.STORAGE.store_browser_profile.assert_not_awaited()
    store_session_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_persisted_to_segmented_key_on_completed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Segmented workflows should write back to the segment-specific storage key."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow.browser_profile_key = "{{ credential_id }}"
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed)
    browser_state = _make_browser_state()
    store_mock = _patch_clean_up_deps(monkeypatch, browser_state)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run_parameters",
        AsyncMock(
            return_value=[
                (
                    SimpleNamespace(key="credential_id"),
                    SimpleNamespace(value="cred_123"),
                )
            ]
        ),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    storage_key = build_workflow_browser_session_storage_key("wpid_test", "cred_123")
    store_mock.assert_awaited_once_with("o_test", storage_key, "/tmp/fake_profile")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        WorkflowRunStatus.failed,
        WorkflowRunStatus.terminated,
        WorkflowRunStatus.canceled,
        WorkflowRunStatus.timed_out,
        WorkflowRunStatus.running,
    ],
)
async def test_profile_not_persisted_on_non_completed_run(
    monkeypatch: pytest.MonkeyPatch,
    status: WorkflowRunStatus,
) -> None:
    """Non-completed runs must NOT write the browser profile back to S3."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(status)
    browser_state = _make_browser_state()
    store_mock = _patch_clean_up_deps(monkeypatch, browser_state)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    store_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_cookies_persisted_before_store_when_browser_stays_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote-browser / existing-session runs keep the browser alive, so close() never persists the
    sidecar; clean_up_workflow must snapshot session cookies before archiving the profile."""
    from skyvern.forge.sdk.workflow import service as service_module
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed)
    workflow_run.browser_address = "ws://remote-browser"
    browser_state = _make_browser_state()
    store_mock = _patch_clean_up_deps(monkeypatch, browser_state)

    order: list[str] = []
    persist_mock = AsyncMock(side_effect=lambda *a, **k: order.append("persist"))
    store_mock.side_effect = lambda *a, **k: order.append("store")
    monkeypatch.setattr(service_module, "persist_session_cookies", persist_mock)

    svc = WorkflowService()
    persist_video_mock = AsyncMock()
    monkeypatch.setattr(svc, "persist_video_data", persist_video_mock)
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    persist_video_mock.assert_awaited_once_with(
        browser_state,
        workflow,
        workflow_run,
        close_browser_on_completion=False,
    )
    persist_mock.assert_awaited_once_with(browser_state.browser_context, "/tmp/fake_profile")
    assert order == ["persist", "store"]


@pytest.mark.asyncio
async def test_session_cookies_not_double_persisted_when_browser_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the browser closes on completion, close() already wrote the sidecar — clean_up_workflow
    must not persist again."""
    from skyvern.forge.sdk.workflow import service as service_module
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed)
    browser_state = _make_browser_state()
    _patch_clean_up_deps(monkeypatch, browser_state)

    persist_mock = AsyncMock()
    monkeypatch.setattr(service_module, "persist_session_cookies", persist_mock)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    persist_mock.assert_not_awaited()


# --- engine-flag branch: flag-off legacy compat + flag-on sink-driven write ---


@pytest.mark.asyncio
async def test_flag_off_persist_on_writes_managed_profile_byte_for_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compat (a): engine OFF + Remember on + a managed profile this workflow owns → the legacy
    own-memory write-back runs exactly as today, and the resolved sink is ignored entirely."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(
        WorkflowRunStatus.completed, browser_profile_id="bp_managed", browser_sink_profile_id="bp_sink_ignored"
    )
    store_session_mock = _patch_clean_up_deps(monkeypatch, _make_browser_state())
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(
            return_value=SimpleNamespace(
                is_managed=True, browser_profile_id="bp_managed", workflow_permanent_id="wpid_test"
            )
        ),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    # The legacy path writes the managed profile (browser_profile_id), NOT the resolved sink.
    app.STORAGE.store_browser_profile.assert_awaited_once_with(
        "o_test", profile_id="bp_managed", directory="/tmp/fake_profile"
    )
    store_session_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_off_credential_suppression_quirk_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compat (b): engine OFF + Remember on + the run seeded a credential's (non-owned) profile → the
    historical own-memory write suppression is preserved untouched; the resolved sink is not consumed."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(
        WorkflowRunStatus.completed, browser_profile_id="bp_cred", browser_sink_profile_id="bp_own"
    )
    store_session_mock = _patch_clean_up_deps(monkeypatch, _make_browser_state())
    # A credential's auto-created profile reads as a plain (unmanaged) profile from the write path's POV.
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False)),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    # Neither the sink (bp_own) nor the legacy archive is written — today's suppression stands.
    app.STORAGE.store_browser_profile.assert_not_awaited()
    store_session_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_on_writes_resolved_sink_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine ON: the run whole-dir writes its resolved sink profile, never re-deriving from the seed."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(
        WorkflowRunStatus.completed, browser_profile_id="bp_seed", browser_sink_profile_id="bp_sink"
    )
    store_session_mock = _patch_clean_up_deps(monkeypatch, _make_browser_state(applied_browser_profile_id="bp_seed"))
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(app.AGENT_FUNCTION, "bank_credential_profile_on_healthy_run", AsyncMock())

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    # The sink (bp_sink) is written, NOT the seed (bp_seed); no legacy archive.
    app.STORAGE.store_browser_profile.assert_awaited_once_with(
        "o_test", profile_id="bp_sink", directory="/tmp/fake_profile"
    )
    store_session_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_on_skips_sink_write_when_stamped_seed_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stamped seed the browser never applied (vendor-routed boot) must not be overwritten by an
    unrelated directory; seedless accumulate rows are unaffected (browser_profile_id None)."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(
        WorkflowRunStatus.completed, browser_profile_id="bp_seed", browser_sink_profile_id="bp_sink"
    )
    _patch_clean_up_deps(monkeypatch, _make_browser_state(applied_browser_profile_id=None))
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(app.AGENT_FUNCTION, "bank_credential_profile_on_healthy_run", AsyncMock())

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    app.STORAGE.store_browser_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_off_start_fresh_run_suppresses_own_memory_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that opted into a fresh browser writes no own-memory back, even flag-off + persist on."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(
        WorkflowRunStatus.completed, browser_profile_id="bp_managed", start_fresh_browser=True
    )
    store_session_mock = _patch_clean_up_deps(monkeypatch, _make_browser_state())
    # A managed profile is stamped, but start_fresh must still suppress the write.
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(
            return_value=SimpleNamespace(
                is_managed=True, browser_profile_id="bp_managed", workflow_permanent_id="wpid_test"
            )
        ),
    )

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    app.STORAGE.store_browser_profile.assert_not_awaited()
    store_session_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_on_no_sink_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine ON + no resolved sink (read-only pick / credential-heal / override / fresh) → the
    workflow writes nothing through this seam; the credential heal engine handles its own writes."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(
        WorkflowRunStatus.completed, browser_profile_id="bp_seed", browser_sink_profile_id=None
    )
    store_session_mock = _patch_clean_up_deps(monkeypatch, _make_browser_state())
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(app.AGENT_FUNCTION, "bank_credential_profile_on_healthy_run", AsyncMock())

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    app.STORAGE.store_browser_profile.assert_not_awaited()
    store_session_mock.assert_not_awaited()


def _make_browser_state_b2(
    *,
    seed_cookies: list[dict] | None,
    seed_etag: str | None,
    fresh_login: bool,
    end_state: list[dict] | None = None,
) -> MagicMock:
    bs = _make_browser_state()
    bs.browser_artifacts._seed_cookie_snapshot = seed_cookies
    bs.browser_artifacts._seed_profile_etag = seed_etag
    bs.browser_artifacts._run_performed_fresh_login = fresh_login
    bs.browser_context.cookies = AsyncMock(return_value=end_state or [])
    return bs


@pytest.mark.asyncio
async def test_sink_changed_true_when_etag_moved_and_no_verified_login(monkeypatch: pytest.MonkeyPatch) -> None:
    # B2: a concurrent write (etag moved) + this run did not freshly log in -> delta-merge path.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    monkeypatch.setattr(app.STORAGE, "get_browser_profile_etag", AsyncMock(return_value="etag_new"))
    bs = _make_browser_state_b2(seed_cookies=[], seed_etag="etag_seed", fresh_login=False)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")
    assert await WorkflowService()._sink_profile_changed_under_run(wr, "bp", bs) is True


@pytest.mark.asyncio
async def test_sink_changed_false_on_verified_login_unchanged_or_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    svc = WorkflowService()
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    # A verified login this run short-circuits to a full write even if the archive moved.
    monkeypatch.setattr(app.STORAGE, "get_browser_profile_etag", AsyncMock(return_value="etag_new"))
    bs_login = _make_browser_state_b2(seed_cookies=[], seed_etag="etag_seed", fresh_login=True)
    assert await svc._sink_profile_changed_under_run(wr, "bp", bs_login) is False

    # Unknown seed fingerprint (no head at seed) -> can't tell -> full write.
    bs_no_seed_etag = _make_browser_state_b2(seed_cookies=[], seed_etag=None, fresh_login=False)
    assert await svc._sink_profile_changed_under_run(wr, "bp", bs_no_seed_etag) is False

    # Unchanged archive -> full write.
    monkeypatch.setattr(app.STORAGE, "get_browser_profile_etag", AsyncMock(return_value="etag_seed"))
    bs_same = _make_browser_state_b2(seed_cookies=[], seed_etag="etag_seed", fresh_login=False)
    assert await svc._sink_profile_changed_under_run(wr, "bp", bs_same) is False

    # Current fingerprint unreadable at write -> don't guess a conflict -> full write.
    monkeypatch.setattr(app.STORAGE, "get_browser_profile_etag", AsyncMock(return_value=None))
    bs_unknown_now = _make_browser_state_b2(seed_cookies=[], seed_etag="etag_seed", fresh_login=False)
    assert await svc._sink_profile_changed_under_run(wr, "bp", bs_unknown_now) is False


@pytest.mark.asyncio
async def test_delta_merge_unions_only_changed_cookies_into_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # B2: only the cookies THIS run changed land in the current stored dir (preserving the concurrent write).
    import json

    from skyvern.forge.sdk.workflow.service import WorkflowService
    from skyvern.webeye.profile_cookie_merge import BANKED_COOKIES_FILENAME

    seed = [{"name": "a", "value": "1", "domain": "x.com", "path": "/"}]
    end_state = [
        {"name": "a", "value": "1", "domain": "x.com", "path": "/"},  # unchanged
        {"name": "b", "value": "2", "domain": "x.com", "path": "/"},  # this run added
    ]
    bs = _make_browser_state_b2(seed_cookies=seed, seed_etag="e", fresh_login=False, end_state=end_state)
    retrieve = AsyncMock(return_value=str(tmp_path))
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_profile", retrieve)
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    assert await WorkflowService()._delta_merge_sink_profile(workflow_run=wr, sink_profile_id="bp", browser_state=bs)
    store.assert_awaited_once()
    assert store.await_args.kwargs["directory"] == str(tmp_path)
    banked = json.loads((tmp_path / BANKED_COOKIES_FILENAME).read_text())  # type: ignore[operator]
    assert {c["name"] for c in banked} == {"b"}


@pytest.mark.asyncio
async def test_delta_merge_no_own_changes_leaves_archive_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # B2: this run changed nothing vs its seed -> don't retrieve or overwrite the concurrent archive.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    seed = [{"name": "a", "value": "1", "domain": "x.com", "path": "/"}]
    bs = _make_browser_state_b2(seed_cookies=seed, seed_etag="e", fresh_login=False, end_state=list(seed))
    retrieve = AsyncMock()
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_profile", retrieve)
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    assert await WorkflowService()._delta_merge_sink_profile(workflow_run=wr, sink_profile_id="bp", browser_state=bs)
    retrieve.assert_not_awaited()
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_delta_merge_falls_back_when_no_seed_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    # B2: without a seed snapshot the delta is unknowable -> return False so the caller does a full write.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    bs = _make_browser_state_b2(seed_cookies=None, seed_etag="e", fresh_login=False)
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    assert (
        await WorkflowService()._delta_merge_sink_profile(workflow_run=wr, sink_profile_id="bp", browser_state=bs)
        is False
    )
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_delta_merge_removes_temp_extraction_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # The retrieved archive is extracted under TEMP_PATH; the merge must not leak it on worker disk.
    import os

    from skyvern.forge.sdk.api.files import make_temp_directory
    from skyvern.forge.sdk.workflow.service import WorkflowService

    seed = [{"name": "a", "value": "1", "domain": "x.com", "path": "/"}]
    end_state = list(seed) + [{"name": "b", "value": "2", "domain": "x.com", "path": "/"}]
    bs = _make_browser_state_b2(seed_cookies=seed, seed_etag="e", fresh_login=False, end_state=end_state)
    temp_dir = make_temp_directory(prefix="sink_merge_test_")
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_profile", AsyncMock(return_value=temp_dir))
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", AsyncMock())
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    assert await WorkflowService()._delta_merge_sink_profile(workflow_run=wr, sink_profile_id="bp", browser_state=bs)
    assert not os.path.exists(temp_dir)  # extraction cleaned up


@pytest.mark.asyncio
async def test_sink_write_skipped_on_etag_storage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Blocker 1: a transient/authz error reading the current fingerprint must SKIP the write, not
    # fail-open (read None as "unchanged") into a full overwrite that could clobber a concurrent write.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    bs = _make_browser_state_b2(seed_cookies=[], seed_etag="seed_etag", fresh_login=False)
    monkeypatch.setattr(app.STORAGE, "get_browser_profile_etag", AsyncMock(side_effect=RuntimeError("s3 blip")))
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    await WorkflowService()._persist_run_sink_profile_if_needed(
        workflow_run=wr,
        browser_state=bs,
        close_browser_on_completion=True,
        effective_workflow_run_status=WorkflowRunStatus.completed,
    )
    store.assert_not_awaited()  # no write happened


@pytest.mark.asyncio
async def test_sink_full_write_on_etag_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # Blocker 1: a genuine not-found returns None (no prior version -> no conflict) -> full write proceeds.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    bs = _make_browser_state_b2(seed_cookies=[], seed_etag="seed_etag", fresh_login=False)
    monkeypatch.setattr(app.STORAGE, "get_browser_profile_etag", AsyncMock(return_value=None))
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    await WorkflowService()._persist_run_sink_profile_if_needed(
        workflow_run=wr,
        browser_state=bs,
        close_browser_on_completion=True,
        effective_workflow_run_status=WorkflowRunStatus.completed,
    )
    store.assert_awaited_once()  # full write


@pytest.mark.asyncio
async def test_delta_merge_reads_session_sidecar_when_context_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # Suggestion 3: on a completed run whose browser is already closed, .cookies() fails; the delta-merge
    # reads the end-state from the session-cookie sidecar close() wrote, instead of skipping to a full write.
    import json

    from skyvern.forge.sdk.workflow.service import WorkflowService
    from skyvern.webeye.session_cookies import SESSION_COOKIES_FILENAME

    seed = [{"name": "a", "value": "1", "domain": "x.com", "path": "/"}]
    end_state = seed + [{"name": "sid", "value": "FRESH", "domain": "x.com", "path": "/"}]
    (tmp_path / SESSION_COOKIES_FILENAME).write_text(json.dumps(end_state))  # type: ignore[operator]
    bs = _make_browser_state_b2(seed_cookies=seed, seed_etag="e", fresh_login=False)
    bs.browser_artifacts.browser_session_dir = str(tmp_path)
    bs.browser_context.cookies = AsyncMock(side_effect=RuntimeError("context closed"))
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_profile", AsyncMock(return_value=str(tmp_path)))
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    assert await WorkflowService()._delta_merge_sink_profile(workflow_run=wr, sink_profile_id="bp", browser_state=bs)
    store.assert_awaited_once()  # delta-merged from the sidecar, not skipped to a clobbering full write


@pytest.mark.asyncio
async def test_sink_writeback_suppressed_when_seed_profile_failed_to_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex P2: a saved profile that fails to launch (corruption/stale lock) falls back to a blank dir;
    # a completed run must NOT write that fallback state back over the seed archive. Control: a normal
    # run (seed loaded) still writes.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    svc = WorkflowService()
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")

    failed = _make_browser_state_b2(seed_cookies=None, seed_etag=None, fresh_login=False)
    failed.browser_artifacts._seed_load_failed = True
    await svc._persist_run_sink_profile_if_needed(
        workflow_run=wr,
        browser_state=failed,
        close_browser_on_completion=True,
        effective_workflow_run_status=WorkflowRunStatus.completed,
    )
    store.assert_not_awaited()  # archive intact — the fallback dir never becomes the profile's state

    ok = _make_browser_state_b2(seed_cookies=None, seed_etag=None, fresh_login=False)
    await svc._persist_run_sink_profile_if_needed(
        workflow_run=wr,
        browser_state=ok,
        close_browser_on_completion=True,
        effective_workflow_run_status=WorkflowRunStatus.completed,
    )
    store.assert_awaited_once()


@pytest.mark.asyncio
async def test_sink_full_write_drops_the_seed_era_banked_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The sidecar restored from the seed archive rides this dir into the sink profile, where every later
    # boot would replay it over a fresher Cookies database. The closed browser's database is authoritative.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    banked = tmp_path / BANKED_COOKIES_FILENAME
    banked.write_text(json.dumps([{"name": "seed_era", "value": "x", "domain": "example.test", "path": "/"}]))
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")
    bs = _make_browser_state_b2(seed_cookies=[], seed_etag=None, fresh_login=False)
    bs.browser_artifacts.browser_session_dir = str(tmp_path)

    await WorkflowService()._persist_run_sink_profile_if_needed(
        workflow_run=wr,
        browser_state=bs,
        close_browser_on_completion=True,
        effective_workflow_run_status=WorkflowRunStatus.completed,
    )

    store.assert_awaited_once()
    assert not banked.exists()


@pytest.mark.asyncio
async def test_sink_writeback_no_full_overwrite_when_seed_capture_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lawy: an uncaptured seed fingerprint is UNKNOWN — the guard must never full-overwrite (a None seed
    # etag must not read as "unchanged"). With no seed snapshot to delta against, skip the write.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")
    bs = _make_browser_state_b2(seed_cookies=None, seed_etag=None, fresh_login=False)
    bs.browser_artifacts._seed_capture_failed = True

    await WorkflowService()._persist_run_sink_profile_if_needed(
        workflow_run=wr,
        browser_state=bs,
        close_browser_on_completion=True,
        effective_workflow_run_status=WorkflowRunStatus.completed,
    )
    store.assert_not_awaited()  # UNKNOWN seed + no snapshot -> skip, never full-overwrite


@pytest.mark.asyncio
async def test_sink_writeback_full_write_when_seed_capture_succeeded_new_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Control: a captured-but-empty seed (new profile, etag legitimately None) is KNOWN, not UNKNOWN — the
    # first full write must still happen.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")
    bs = _make_browser_state_b2(seed_cookies=[], seed_etag=None, fresh_login=False)  # capture ran, no archive

    await WorkflowService()._persist_run_sink_profile_if_needed(
        workflow_run=wr,
        browser_state=bs,
        close_browser_on_completion=True,
        effective_workflow_run_status=WorkflowRunStatus.completed,
    )
    store.assert_awaited_once()


@pytest.mark.asyncio
async def test_sink_writeback_skips_full_write_when_changed_but_merge_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # claude bot: when the sink moved under the run (changed=True) but the delta-merge can't run
    # (retrieve swallowed a transient error to None / empty sidecar), a full write would clobber the
    # concurrent writer's state — the caller must SKIP, not fall through to store_browser_profile.
    from skyvern.forge.sdk.workflow.service import WorkflowService

    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    svc = WorkflowService()
    monkeypatch.setattr(svc, "_sink_profile_changed_under_run", AsyncMock(return_value=True))
    monkeypatch.setattr(svc, "_delta_merge_sink_profile", AsyncMock(return_value=False))
    wr = _make_workflow_run(WorkflowRunStatus.completed, browser_sink_profile_id="bp")
    bs = _make_browser_state_b2(seed_cookies=[], seed_etag="e", fresh_login=False)

    await svc._persist_run_sink_profile_if_needed(
        workflow_run=wr,
        browser_state=bs,
        close_browser_on_completion=True,
        effective_workflow_run_status=WorkflowRunStatus.completed,
    )
    store.assert_not_awaited()  # sink moved + merge unavailable -> skip, never clobber the concurrent write
