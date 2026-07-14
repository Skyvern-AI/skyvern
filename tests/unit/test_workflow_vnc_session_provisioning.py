from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import (
    CODE_BLOCK_SESSION_TIMEOUT_MINUTES,
    WorkflowService,
    WorkflowVncSessionSetupState,
)
from skyvern.webeye.async_utils import await_to_terminal_state
from skyvern.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager
from tests.unit.test_workflow_service_managed_browser_profile import (
    _execute_workflow,
    _execute_workflow_run,
    _patch_browser_cleanup,
    _patch_execute_workflow_deps,
    _patch_finalize,
)


@dataclass(frozen=True)
class ProvisioningCase:
    case_id: str
    mode: str
    default_manager: bool
    blocks: tuple[str, ...]
    browser_session_id: str | None = None
    browser_profile_id: str | None = None
    managed_profile: bool = False
    code_gate: bool = False
    expected_creations: int = 0
    expected_timeout_minutes: int | None = None
    expected_profile_id: str | None = None
    expected_code_gate_calls: int = 0


CASES = [
    ProvisioningCase("W1", "vnc", True, (), expected_creations=1, expected_timeout_minutes=60),
    ProvisioningCase(
        "W2",
        "vnc",
        True,
        (),
        browser_profile_id="bp_user",
        expected_creations=1,
        expected_timeout_minutes=60,
        expected_profile_id="bp_user",
    ),
    ProvisioningCase(
        "W3",
        "vnc",
        True,
        ("hi",),
        browser_profile_id="bp_managed",
        managed_profile=True,
        expected_creations=1,
        expected_timeout_minutes=61,
        expected_profile_id="bp_managed",
    ),
    ProvisioningCase(
        "W4",
        "vnc",
        True,
        ("code",),
        code_gate=True,
        expected_creations=1,
        expected_timeout_minutes=60,
    ),
    ProvisioningCase(
        "W5",
        "vnc",
        True,
        ("hi", "code"),
        code_gate=True,
        expected_creations=1,
        expected_timeout_minutes=61,
    ),
    ProvisioningCase(
        "W6",
        "vnc",
        True,
        ("hi", "code"),
        browser_session_id="pbs_supplied",
        browser_profile_id="bp_user",
        code_gate=True,
    ),
    ProvisioningCase("W9", "vnc", False, ()),
    ProvisioningCase(
        "W10",
        "cdp",
        True,
        ("hi",),
        expected_creations=1,
        expected_timeout_minutes=61,
    ),
    ProvisioningCase(
        "W11",
        "cdp",
        True,
        ("hi",),
        browser_profile_id="bp_user",
    ),
    ProvisioningCase(
        "W12",
        "cdp",
        True,
        ("hi",),
        browser_profile_id="bp_managed",
        managed_profile=True,
        expected_creations=1,
        expected_timeout_minutes=61,
        expected_profile_id="bp_managed",
    ),
    ProvisioningCase(
        "W13",
        "cdp",
        True,
        ("code",),
        code_gate=True,
        expected_creations=1,
        expected_timeout_minutes=CODE_BLOCK_SESSION_TIMEOUT_MINUTES,
        expected_code_gate_calls=1,
    ),
    ProvisioningCase("W14", "cdp", True, ()),
]


def _workflow(block_names: tuple[str, ...]) -> SimpleNamespace:
    blocks = []
    for block_name in block_names:
        if block_name == "hi":
            blocks.append(SimpleNamespace(block_type=BlockType.HUMAN_INTERACTION, timeout_seconds=60))
        elif block_name == "code":
            blocks.append(SimpleNamespace(block_type=BlockType.CODE))
        else:
            raise AssertionError(f"Unknown test block {block_name}")

    return SimpleNamespace(
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_definition=SimpleNamespace(blocks=blocks),
    )


def _install_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    use_default: bool,
) -> tuple[object, AsyncMock]:
    created_session = SimpleNamespace(persistent_browser_session_id="pbs_created")
    create_session = AsyncMock(return_value=created_session)
    if use_default:
        monkeypatch.setattr(DefaultPersistentSessionsManager, "instance", None)
        manager: object = DefaultPersistentSessionsManager(MagicMock())
    else:
        manager = SimpleNamespace()
    monkeypatch.setattr(manager, "create_session", create_session, raising=False)
    monkeypatch.setattr(service_module.app, "PERSISTENT_SESSIONS_MANAGER", manager)
    return manager, create_session


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
@pytest.mark.asyncio
async def test_workflow_session_provisioning_matrix(
    monkeypatch: pytest.MonkeyPatch,
    case: ProvisioningCase,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", case.mode)
    _, create_session = _install_manager(monkeypatch, use_default=case.default_manager)
    code_gate = AsyncMock(return_value=case.code_gate)
    monkeypatch.setattr(
        service_module.app.AGENT_FUNCTION,
        "should_auto_create_browser_session_for_code_block",
        code_gate,
    )
    service = WorkflowService()
    profile_is_managed = AsyncMock(return_value=case.managed_profile)
    monkeypatch.setattr(service, "_browser_profile_is_managed", profile_is_managed)

    result = await service._auto_create_workflow_browser_session_if_needed(
        organization_id="org_test",
        workflow=_workflow(case.blocks),
        workflow_run_id="wr_test",
        browser_session_id=case.browser_session_id,
        browser_profile_id=case.browser_profile_id,
        proxy_location=None,
    )

    assert create_session.await_count == case.expected_creations
    assert code_gate.await_count == case.expected_code_gate_calls

    if case.expected_creations:
        assert result is not None
        create_session.assert_awaited_once_with(
            organization_id="org_test",
            timeout_minutes=case.expected_timeout_minutes,
            browser_profile_id=case.expected_profile_id,
            proxy_location=None,
            inherit_profile_proxy=True,
        )
    else:
        assert result is None

    if case.browser_session_id:
        profile_is_managed.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_vnc_creation_error_does_not_fall_through_to_code_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    _, create_session = _install_manager(monkeypatch, use_default=True)
    create_session.side_effect = RuntimeError("session creation failed")
    code_gate = AsyncMock(return_value=True)
    monkeypatch.setattr(
        service_module.app.AGENT_FUNCTION,
        "should_auto_create_browser_session_for_code_block",
        code_gate,
    )

    with pytest.raises(RuntimeError, match="session creation failed"):
        await WorkflowService()._auto_create_workflow_browser_session_if_needed(
            organization_id="org_test",
            workflow=_workflow(("code",)),
            workflow_run_id="wr_test",
            browser_session_id=None,
            browser_profile_id=None,
            proxy_location=None,
        )

    assert create_session.await_count == 1
    code_gate.assert_not_awaited()


@pytest.mark.parametrize("mode", ["vnc", "cdp"])
@pytest.mark.parametrize(
    ("caller_session_id", "row_session_id", "expected_session_id", "expected_close", "expects_warning"),
    [
        (None, "pbs_forced", "pbs_forced", True, False),
        ("", "pbs_forced", "pbs_forced", True, False),
        ("pbs_caller", "pbs_forced", "pbs_caller", False, True),
        ("pbs_caller", "", "pbs_caller", False, False),
    ],
    ids=["W7-adopt-forced", "W7-empty-adopts-forced", "W8-caller-wins", "W8-empty-row-no-warning"],
)
@pytest.mark.asyncio
async def test_execute_workflow_adopts_persisted_session_and_preserves_original_ownership(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    caller_session_id: str | None,
    row_session_id: str,
    expected_session_id: str,
    expected_close: bool,
    expects_warning: bool,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", mode)
    _, create_session = _install_manager(monkeypatch, use_default=True)
    manager = service_module.app.PERSISTENT_SESSIONS_MANAGER
    begin_session = AsyncMock()
    monkeypatch.setattr(manager, "begin_session", begin_session)

    workflow = _execute_workflow()
    workflow.persist_browser_session = False
    created_run = _execute_workflow_run(WorkflowRunStatus.created)
    created_run.browser_session_id = row_session_id
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    completed_run.browser_session_id = row_session_id
    service = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, service, workflow, completed_run)
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(
        service,
        "auto_create_browser_session_if_needed",
        WorkflowService.auto_create_browser_session_if_needed.__get__(service),
    )
    monkeypatch.setattr(
        service,
        "auto_create_browser_session_for_code_block_if_needed",
        WorkflowService.auto_create_browser_session_for_code_block_if_needed.__get__(service),
    )

    context = SimpleNamespace(browser_session_id=None)
    monkeypatch.setattr(
        service_module.app.WORKFLOW_CONTEXT_MANAGER,
        "get_workflow_run_context",
        lambda _workflow_run_id: context,
    )
    order: list[str] = []
    clean_up_browser = _patch_browser_cleanup(monkeypatch, service, order)
    _patch_finalize(monkeypatch, service, order, completed_run)
    monkeypatch.setattr(service, "_persist_workflow_browser_session_if_needed", AsyncMock())
    monkeypatch.setattr(service, "persist_video_data", AsyncMock())
    monkeypatch.setattr(service, "execute_workflow_webhook", AsyncMock())
    log = MagicMock()
    monkeypatch.setattr(service_module, "LOG", log)

    result = await service.execute_workflow(
        workflow_run_id="wr_test",
        api_key=None,
        organization=SimpleNamespace(organization_id="o_test"),
        browser_session_id=caller_session_id,
        need_call_webhook=False,
    )

    assert result is completed_run
    assert create_session.await_count == 0
    assert context.browser_session_id == expected_session_id
    begin_session.assert_awaited_once_with(
        browser_session_id=expected_session_id,
        runnable_type="workflow_run",
        runnable_id="wr_test",
        organization_id="o_test",
    )
    clean_up_browser.assert_awaited_once()
    assert clean_up_browser.await_args.kwargs["browser_session_id"] == expected_session_id
    assert clean_up_browser.await_args.kwargs["close_browser_on_completion"] is expected_close
    service_module.app.DATABASE.workflow_runs.update_workflow_run.assert_not_awaited()

    warning = "Workflow execution browser session differs from persisted workflow-run session; keeping caller session."
    if expects_warning:
        log.warning.assert_any_call(
            warning,
            workflow_run_id="wr_test",
            organization_id="o_test",
            caller_browser_session_id="pbs_caller",
            persisted_browser_session_id="pbs_forced",
        )
    else:
        assert not any(call.args == (warning,) for call in log.warning.call_args_list)


@pytest.mark.parametrize(
    ("browser_address", "expected_close"),
    [
        (None, True),
        ("wss://remote.example/devtools/browser/id", False),
    ],
)
@pytest.mark.asyncio
async def test_cleanup_preserves_session_ownership_unless_browser_is_remote(
    monkeypatch: pytest.MonkeyPatch,
    browser_address: str | None,
    expected_close: bool,
) -> None:
    service = WorkflowService()
    monkeypatch.setattr(service, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))
    get_children = AsyncMock(return_value=[])
    cleanup_for_workflow_run = AsyncMock(return_value=None)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        get_children,
    )
    monkeypatch.setattr(
        service_module.app.BROWSER_MANAGER,
        "cleanup_for_workflow_run",
        cleanup_for_workflow_run,
    )
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_test",
        organization_id="org_test",
        browser_address=browser_address,
    )

    result = await service._clean_up_workflow_browser(
        workflow_run,
        close_browser_on_completion=True,
        browser_session_id="pbs_owned",
    )

    assert result.close_browser_on_completion is expected_close
    cleanup_for_workflow_run.assert_awaited_once_with(
        "wr_test",
        [],
        close_browser_on_completion=expected_close,
        browser_session_id="pbs_owned",
        organization_id="org_test",
        child_workflow_run_ids=[],
    )


@pytest.mark.asyncio
async def test_default_vnc_process_lock_serializes_concurrent_workflow_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, create_session = _install_manager(monkeypatch, use_default=True)
    close_session = AsyncMock()
    monkeypatch.setattr(manager, "close_session", close_session)
    begin_session = AsyncMock()
    monkeypatch.setattr(manager, "begin_session", begin_session)
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})
    context = SimpleNamespace(browser_session_id=None)
    monkeypatch.setattr(
        service_module.app.WORKFLOW_CONTEXT_MANAGER,
        "get_workflow_run_context",
        lambda _workflow_run_id: context,
    )

    row_session_id: str | None = None

    async def get_workflow_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(browser_session_id=row_session_id)

    async def claim_workflow_run_browser_session(**kwargs: str) -> SimpleNamespace:
        nonlocal row_session_id
        await asyncio.sleep(0)
        if row_session_id:
            return SimpleNamespace(browser_session_id=row_session_id, installed=False)
        row_session_id = kwargs["candidate_browser_session_id"]
        return SimpleNamespace(browser_session_id=row_session_id, installed=True)

    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(side_effect=get_workflow_run),
    )
    claim = AsyncMock(side_effect=claim_workflow_run_browser_session)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        claim,
        raising=False,
    )

    service = WorkflowService()
    states = [WorkflowVncSessionSetupState(), WorkflowVncSessionSetupState()]
    await asyncio.gather(
        *(
            service._set_up_default_vnc_workflow_browser_session(
                organization_id="org_test",
                workflow=_workflow(()),
                workflow_run_id="wr_test",
                caller_supplied_browser_session=False,
                browser_profile_id=None,
                proxy_location=None,
                state=state,
            )
            for state in states
        )
    )

    assert [state.effective_browser_session_id for state in states] == ["pbs_created", "pbs_created"]
    assert create_session.await_count == 1
    assert claim.await_count == 1
    assert begin_session.await_count == 2
    assert context.browser_session_id == "pbs_created"
    close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_vnc_cancelled_installer_closes_owned_candidate_even_with_queued_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, create_session = _install_manager(monkeypatch, use_default=True)
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})

    row_session_id: str | None = None
    first_begin_started = asyncio.Event()
    finish_first_begin = asyncio.Event()
    begin_count = 0

    async def begin_session(**_kwargs: str) -> None:
        nonlocal begin_count
        begin_count += 1
        if begin_count == 1:
            first_begin_started.set()
            await finish_first_begin.wait()

    async def get_workflow_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(browser_session_id=row_session_id)

    async def claim_workflow_run_browser_session(**kwargs: str) -> SimpleNamespace:
        nonlocal row_session_id
        if row_session_id:
            return SimpleNamespace(browser_session_id=row_session_id, installed=False)
        row_session_id = kwargs["candidate_browser_session_id"]
        return SimpleNamespace(browser_session_id=row_session_id, installed=True)

    close_session = AsyncMock()
    release_browser_session = AsyncMock()
    monkeypatch.setattr(manager, "begin_session", AsyncMock(side_effect=begin_session))
    monkeypatch.setattr(manager, "close_session", close_session)
    monkeypatch.setattr(manager, "release_browser_session", release_browser_session)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(side_effect=get_workflow_run),
    )
    claim = AsyncMock(side_effect=claim_workflow_run_browser_session)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        claim,
        raising=False,
    )
    context = SimpleNamespace(browser_session_id=None)
    monkeypatch.setattr(
        service_module.app.WORKFLOW_CONTEXT_MANAGER,
        "get_workflow_run_context",
        lambda _workflow_run_id: context,
    )
    monkeypatch.setattr(
        service_module.app.WORKFLOW_CONTEXT_MANAGER,
        "remove_workflow_run_context",
        MagicMock(),
    )

    service = WorkflowService()
    installer_state = WorkflowVncSessionSetupState()
    adopter_state = WorkflowVncSessionSetupState()

    async def installer_delivery() -> None:
        try:
            await await_to_terminal_state(
                service._set_up_default_vnc_workflow_browser_session(
                    organization_id="org_test",
                    workflow=_workflow(()),
                    workflow_run_id="wr_test",
                    caller_supplied_browser_session=False,
                    browser_profile_id=None,
                    proxy_location=None,
                    state=installer_state,
                )
            )
        except BaseException:
            await await_to_terminal_state(
                service._roll_back_default_vnc_workflow_session_setup(
                    organization_id="org_test",
                    workflow_run_id="wr_test",
                    state=installer_state,
                )
            )
            raise

    installer = asyncio.create_task(installer_delivery())
    await first_begin_started.wait()
    installer.cancel()
    adopter = asyncio.create_task(
        service._set_up_default_vnc_workflow_browser_session(
            organization_id="org_test",
            workflow=_workflow(()),
            workflow_run_id="wr_test",
            caller_supplied_browser_session=False,
            browser_profile_id=None,
            proxy_location=None,
            state=adopter_state,
        )
    )
    await asyncio.sleep(0)
    finish_first_begin.set()

    await adopter
    with pytest.raises(asyncio.CancelledError):
        await installer

    assert installer_state.candidate_installed is True
    assert adopter_state.effective_browser_session_id == "pbs_created"
    assert context.browser_session_id == "pbs_created"
    assert begin_count == 2
    create_session.assert_awaited_once()
    claim.assert_awaited_once()
    close_session.assert_awaited_once_with("org_test", "pbs_created")
    release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_vnc_cas_loser_is_closed_and_persisted_winner_is_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, create_session = _install_manager(monkeypatch, use_default=True)
    close_session = AsyncMock()
    monkeypatch.setattr(manager, "close_session", close_session)
    order: list[str] = []
    close_session.side_effect = lambda *_args: order.append("close-loser")
    begin_session = AsyncMock(side_effect=lambda **_kwargs: order.append("begin-winner"))
    monkeypatch.setattr(manager, "begin_session", begin_session)
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})
    context = SimpleNamespace(browser_session_id=None)
    monkeypatch.setattr(
        service_module.app.WORKFLOW_CONTEXT_MANAGER,
        "get_workflow_run_context",
        lambda _workflow_run_id: context,
    )
    get_workflow_run = AsyncMock(
        side_effect=[
            SimpleNamespace(browser_session_id=None),
            SimpleNamespace(browser_session_id="pbs_winner"),
        ]
    )
    monkeypatch.setattr(service_module.app.DATABASE.workflow_runs, "get_workflow_run", get_workflow_run)
    claim = AsyncMock(return_value=SimpleNamespace(browser_session_id="pbs_winner", installed=False))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        claim,
        raising=False,
    )

    state = WorkflowVncSessionSetupState()
    await WorkflowService()._set_up_default_vnc_workflow_browser_session(
        organization_id="org_test",
        workflow=_workflow(()),
        workflow_run_id="wr_test",
        caller_supplied_browser_session=False,
        browser_profile_id=None,
        proxy_location=None,
        state=state,
    )

    assert state.effective_browser_session_id == "pbs_winner"
    assert context.browser_session_id == "pbs_winner"
    create_session.assert_awaited_once()
    claim.assert_awaited_once_with(
        workflow_run_id="wr_test",
        organization_id="org_test",
        candidate_browser_session_id="pbs_created",
    )
    close_session.assert_awaited_once_with("org_test", "pbs_created")
    begin_session.assert_awaited_once_with(
        browser_session_id="pbs_winner",
        runnable_type="workflow_run",
        runnable_id="wr_test",
        organization_id="org_test",
    )
    assert order == ["close-loser", "begin-winner"]


@pytest.mark.asyncio
async def test_default_vnc_claim_failure_closes_unattached_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, _create_session = _install_manager(monkeypatch, use_default=True)
    close_session = AsyncMock()
    monkeypatch.setattr(manager, "close_session", close_session)
    begin_session = AsyncMock()
    monkeypatch.setattr(manager, "begin_session", begin_session)
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            side_effect=[
                SimpleNamespace(browser_session_id=None),
                SimpleNamespace(browser_session_id=None),
            ]
        ),
    )
    claim_error = RuntimeError("claim write failed")
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        AsyncMock(side_effect=claim_error),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="claim write failed") as exc_info:
        await WorkflowService()._set_up_default_vnc_workflow_browser_session(
            organization_id="org_test",
            workflow=_workflow(()),
            workflow_run_id="wr_test",
            caller_supplied_browser_session=False,
            browser_profile_id=None,
            proxy_location=None,
            state=WorkflowVncSessionSetupState(),
        )

    assert exc_info.value is claim_error
    close_session.assert_awaited_once_with("org_test", "pbs_created")
    begin_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_vnc_ambiguous_claim_error_adopts_installed_candidate_without_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, _create_session = _install_manager(monkeypatch, use_default=True)
    close_session = AsyncMock()
    begin_session = AsyncMock()
    monkeypatch.setattr(manager, "close_session", close_session)
    monkeypatch.setattr(manager, "begin_session", begin_session)
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})
    context = SimpleNamespace(browser_session_id=None)
    monkeypatch.setattr(
        service_module.app.WORKFLOW_CONTEXT_MANAGER,
        "get_workflow_run_context",
        lambda _workflow_run_id: context,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            side_effect=[
                SimpleNamespace(browser_session_id=None),
                SimpleNamespace(browser_session_id="pbs_created"),
            ]
        ),
    )
    claim_error = RuntimeError("commit result unavailable")
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        AsyncMock(side_effect=claim_error),
        raising=False,
    )

    state = WorkflowVncSessionSetupState()
    await WorkflowService()._set_up_default_vnc_workflow_browser_session(
        organization_id="org_test",
        workflow=_workflow(()),
        workflow_run_id="wr_test",
        caller_supplied_browser_session=False,
        browser_profile_id=None,
        proxy_location=None,
        state=state,
    )

    assert state.effective_browser_session_id == "pbs_created"
    assert state.candidate_installed is True
    assert context.browser_session_id == "pbs_created"
    close_session.assert_not_awaited()
    begin_session.assert_awaited_once_with(
        browser_session_id="pbs_created",
        runnable_type="workflow_run",
        runnable_id="wr_test",
        organization_id="org_test",
    )


@pytest.mark.asyncio
async def test_default_vnc_ambiguous_claim_error_closes_candidate_and_adopts_other_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, _create_session = _install_manager(monkeypatch, use_default=True)
    order: list[str] = []
    close_session = AsyncMock(side_effect=lambda *_args: order.append("close-loser"))
    begin_session = AsyncMock(side_effect=lambda **_kwargs: order.append("begin-winner"))
    monkeypatch.setattr(manager, "close_session", close_session)
    monkeypatch.setattr(manager, "begin_session", begin_session)
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})
    context = SimpleNamespace(browser_session_id=None)
    monkeypatch.setattr(
        service_module.app.WORKFLOW_CONTEXT_MANAGER,
        "get_workflow_run_context",
        lambda _workflow_run_id: context,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            side_effect=[
                SimpleNamespace(browser_session_id=None),
                SimpleNamespace(browser_session_id="pbs_winner"),
            ]
        ),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        AsyncMock(side_effect=RuntimeError("commit result unavailable")),
        raising=False,
    )

    state = WorkflowVncSessionSetupState()
    await WorkflowService()._set_up_default_vnc_workflow_browser_session(
        organization_id="org_test",
        workflow=_workflow(()),
        workflow_run_id="wr_test",
        caller_supplied_browser_session=False,
        browser_profile_id=None,
        proxy_location=None,
        state=state,
    )

    assert state.effective_browser_session_id == "pbs_winner"
    assert state.candidate_closed is True
    assert context.browser_session_id == "pbs_winner"
    assert order == ["close-loser", "begin-winner"]


@pytest.mark.parametrize(
    ("reconciliation_outcome", "expected_reconciliation_note"),
    [
        pytest.param(None, False, id="missing-row"),
        pytest.param(RuntimeError("reconciliation read failed"), True, id="read-failure"),
    ],
)
@pytest.mark.asyncio
async def test_default_vnc_claim_reconciliation_failure_closes_candidate_and_propagates_claim_error(
    monkeypatch: pytest.MonkeyPatch,
    reconciliation_outcome: object,
    expected_reconciliation_note: bool,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, _create_session = _install_manager(monkeypatch, use_default=True)
    close_session = AsyncMock()
    begin_session = AsyncMock()
    monkeypatch.setattr(manager, "close_session", close_session)
    monkeypatch.setattr(manager, "begin_session", begin_session)
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            side_effect=[
                SimpleNamespace(browser_session_id=None),
                reconciliation_outcome,
            ]
        ),
    )
    claim_error = RuntimeError("claim result unavailable")
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        AsyncMock(side_effect=claim_error),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="claim result unavailable") as exc_info:
        await WorkflowService()._set_up_default_vnc_workflow_browser_session(
            organization_id="org_test",
            workflow=_workflow(()),
            workflow_run_id="wr_test",
            caller_supplied_browser_session=False,
            browser_profile_id=None,
            proxy_location=None,
            state=WorkflowVncSessionSetupState(),
        )

    assert exc_info.value is claim_error
    reconciliation_notes = [
        note for note in getattr(claim_error, "__notes__", []) if "claim reconciliation error" in note
    ]
    assert bool(reconciliation_notes) is expected_reconciliation_note
    close_session.assert_awaited_once_with("org_test", "pbs_created")
    begin_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_outer_cancellation_after_candidate_allocation_waits_for_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, create_session = _install_manager(monkeypatch, use_default=True)
    candidate_allocated = asyncio.Event()
    finish_create = asyncio.Event()
    close_started = asyncio.Event()
    finish_close = asyncio.Event()

    async def create_candidate(**_kwargs: object) -> SimpleNamespace:
        candidate_allocated.set()
        await finish_create.wait()
        return SimpleNamespace(persistent_browser_session_id="pbs_created")

    async def close_session(_organization_id: str, _browser_session_id: str) -> None:
        close_started.set()
        await finish_close.wait()

    create_session.side_effect = create_candidate
    claim = AsyncMock(return_value=SimpleNamespace(browser_session_id="pbs_created", installed=True))
    begin_session = AsyncMock()
    monkeypatch.setattr(manager, "begin_session", begin_session)
    monkeypatch.setattr(manager, "close_session", AsyncMock(side_effect=close_session))
    monkeypatch.setattr(manager, "release_browser_session", AsyncMock())
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})

    workflow = _execute_workflow()
    workflow.persist_browser_session = False
    created_run = _execute_workflow_run(WorkflowRunStatus.created)
    service = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, service, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(
        service,
        "auto_create_browser_session_if_needed",
        WorkflowService.auto_create_browser_session_if_needed.__get__(service),
    )
    monkeypatch.setattr(
        service,
        "auto_create_browser_session_for_code_block_if_needed",
        WorkflowService.auto_create_browser_session_for_code_block_if_needed.__get__(service),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=created_run),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        claim,
        raising=False,
    )

    execute_task = asyncio.create_task(
        service.execute_workflow(
            workflow_run_id="wr_test",
            api_key=None,
            organization=SimpleNamespace(organization_id="o_test"),
            need_call_webhook=False,
        )
    )
    await candidate_allocated.wait()
    execute_task.cancel()
    await asyncio.sleep(0)
    assert not execute_task.done()

    finish_create.set()
    await close_started.wait()
    assert not execute_task.done()
    finish_close.set()

    with pytest.raises(asyncio.CancelledError):
        await execute_task

    claim.assert_awaited_once()
    begin_session.assert_awaited_once()
    manager.close_session.assert_awaited_once_with("o_test", "pbs_created")
    manager.release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_outer_cancellation_during_claim_waits_for_owned_candidate_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, _create_session = _install_manager(monkeypatch, use_default=True)
    claim_started = asyncio.Event()
    finish_claim = asyncio.Event()
    close_started = asyncio.Event()
    finish_close = asyncio.Event()

    async def claim_workflow_run_browser_session(**_kwargs: str) -> SimpleNamespace:
        claim_started.set()
        await finish_claim.wait()
        return SimpleNamespace(browser_session_id="pbs_created", installed=True)

    async def close_session(_organization_id: str, _browser_session_id: str) -> None:
        close_started.set()
        await finish_close.wait()

    monkeypatch.setattr(manager, "begin_session", AsyncMock())
    monkeypatch.setattr(manager, "close_session", AsyncMock(side_effect=close_session))
    monkeypatch.setattr(manager, "release_browser_session", AsyncMock())
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})

    workflow = _execute_workflow()
    workflow.persist_browser_session = False
    created_run = _execute_workflow_run(WorkflowRunStatus.created)
    service = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, service, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(
        service,
        "auto_create_browser_session_if_needed",
        WorkflowService.auto_create_browser_session_if_needed.__get__(service),
    )
    monkeypatch.setattr(
        service,
        "auto_create_browser_session_for_code_block_if_needed",
        WorkflowService.auto_create_browser_session_for_code_block_if_needed.__get__(service),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=created_run),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        AsyncMock(side_effect=claim_workflow_run_browser_session),
        raising=False,
    )

    execute_task = asyncio.create_task(
        service.execute_workflow(
            workflow_run_id="wr_test",
            api_key=None,
            organization=SimpleNamespace(organization_id="o_test"),
            need_call_webhook=False,
        )
    )
    await claim_started.wait()
    execute_task.cancel()
    await asyncio.sleep(0)
    assert not execute_task.done()

    finish_claim.set()
    await close_started.wait()
    assert not execute_task.done()
    finish_close.set()

    with pytest.raises(asyncio.CancelledError):
        await execute_task

    manager.close_session.assert_awaited_once_with("o_test", "pbs_created")
    manager.release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_cancellation_after_fresh_candidate_occupation_closes_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, create_session = _install_manager(monkeypatch, use_default=True)
    occupied = asyncio.Event()
    finish_begin = asyncio.Event()
    close_started = asyncio.Event()
    finish_close = asyncio.Event()

    async def begin_then_wait(**_kwargs: str) -> None:
        occupied.set()
        await finish_begin.wait()

    async def close_then_wait(_organization_id: str, _browser_session_id: str) -> None:
        close_started.set()
        await finish_close.wait()

    claim = AsyncMock(return_value=SimpleNamespace(browser_session_id="pbs_created", installed=True))
    begin_session = AsyncMock(side_effect=begin_then_wait)
    close_session = AsyncMock(side_effect=close_then_wait)
    release_browser_session = AsyncMock()
    monkeypatch.setattr(manager, "begin_session", begin_session)
    monkeypatch.setattr(manager, "close_session", close_session)
    monkeypatch.setattr(manager, "release_browser_session", release_browser_session)
    monkeypatch.setattr(WorkflowService, "_vnc_workflow_session_locks", {})

    workflow = _execute_workflow()
    workflow.persist_browser_session = False
    created_run = _execute_workflow_run(WorkflowRunStatus.created)
    service = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, service, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(
        service,
        "auto_create_browser_session_if_needed",
        WorkflowService.auto_create_browser_session_if_needed.__get__(service),
    )
    monkeypatch.setattr(
        service,
        "auto_create_browser_session_for_code_block_if_needed",
        WorkflowService.auto_create_browser_session_for_code_block_if_needed.__get__(service),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=created_run),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "claim_workflow_run_browser_session",
        claim,
        raising=False,
    )
    remove_context = MagicMock()
    monkeypatch.setattr(
        service_module.app.WORKFLOW_CONTEXT_MANAGER,
        "remove_workflow_run_context",
        remove_context,
    )

    execute_task = asyncio.create_task(
        service.execute_workflow(
            workflow_run_id="wr_test",
            api_key=None,
            organization=SimpleNamespace(organization_id="o_test"),
            need_call_webhook=False,
        )
    )
    await occupied.wait()
    execute_task.cancel()
    await asyncio.sleep(0)
    assert not execute_task.done()

    finish_begin.set()
    await close_started.wait()
    assert not execute_task.done()
    finish_close.set()

    with pytest.raises(asyncio.CancelledError):
        await execute_task

    create_session.assert_awaited_once()
    claim.assert_awaited_once_with(
        workflow_run_id="wr_test",
        organization_id="o_test",
        candidate_browser_session_id="pbs_created",
    )
    begin_session.assert_awaited_once_with(
        browser_session_id="pbs_created",
        runnable_type="workflow_run",
        runnable_id="wr_test",
        organization_id="o_test",
    )
    close_session.assert_awaited_once_with("o_test", "pbs_created")
    release_browser_session.assert_not_awaited()
    remove_context.assert_called_once_with("wr_test")


@pytest.mark.asyncio
async def test_execute_workflow_cancellation_after_occupation_releases_persisted_incumbent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    manager, create_session = _install_manager(monkeypatch, use_default=True)
    occupied = asyncio.Event()
    finish_begin = asyncio.Event()
    released = asyncio.Event()

    async def begin_then_wait(**_kwargs: str) -> None:
        occupied.set()
        await finish_begin.wait()

    begin_session = AsyncMock(side_effect=begin_then_wait)
    monkeypatch.setattr(manager, "begin_session", begin_session)
    release_browser_session = AsyncMock(side_effect=lambda *_args, **_kwargs: released.set())
    close_session = AsyncMock()
    monkeypatch.setattr(manager, "release_browser_session", release_browser_session)
    monkeypatch.setattr(manager, "close_session", close_session)
    workflow = _execute_workflow()
    workflow.persist_browser_session = False
    created_run = _execute_workflow_run(WorkflowRunStatus.created)
    created_run.browser_session_id = "pbs_owned"
    service = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, service, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=created_run)
    )

    execute_task = asyncio.create_task(
        service.execute_workflow(
            workflow_run_id="wr_test",
            api_key=None,
            organization=SimpleNamespace(organization_id="o_test"),
            need_call_webhook=False,
        )
    )
    await occupied.wait()
    execute_task.cancel()
    await asyncio.sleep(0)
    assert not execute_task.done()
    finish_begin.set()

    with pytest.raises(asyncio.CancelledError):
        await execute_task

    assert occupied.is_set()
    assert released.is_set()
    create_session.assert_not_awaited()
    close_session.assert_not_awaited()
    release_browser_session.assert_awaited_once_with("pbs_owned", "o_test")
    begin_session.assert_awaited_once_with(
        browser_session_id="pbs_owned",
        runnable_type="workflow_run",
        runnable_id="wr_test",
        organization_id="o_test",
    )
