from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile
from skyvern.forge.sdk.schemas.persistent_browser_sessions import FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE
from skyvern.forge.sdk.workflow.browser_profile_key import build_browser_profile_key_digest
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.proxy_pinning import derive_proxy_session_id, is_proxy_session_id
from skyvern.schemas.runs import ProxyLocation
from tests.unit.conftest import MockAsyncSessionCtx


def _workflow(
    *,
    persist_browser_session: bool = True,
    pin_saved_session_ip: bool = True,
    browser_profile_key: str | None = None,
    proxy_location: ProxyLocation | None = ProxyLocation.RESIDENTIAL_ISP,
    parameters: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        persist_browser_session=persist_browser_session,
        pin_saved_session_ip=pin_saved_session_ip,
        browser_profile_key=browser_profile_key,
        proxy_location=proxy_location,
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        organization_id="org_test",
        title="Workflow",
        webhook_callback_url=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_profile_id=None,
        max_elapsed_time_minutes=None,
        run_with="agent",
        code_version=None,
        adaptive_caching=False,
        sequential_key=None,
        workflow_definition=SimpleNamespace(parameters=parameters or []),
    )


def _workflow_parameter(key: str, default_value: Any = None) -> WorkflowParameter:
    now = datetime.now(UTC)
    return WorkflowParameter(
        workflow_parameter_id=f"wfp_{key}",
        workflow_id="wf_test",
        key=key,
        workflow_parameter_type=WorkflowParameterType.STRING,
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


def _credential_parameter(
    key: str,
    *,
    credential_id: str = "cred_default",
    credential_ids: list[str] | None = None,
    selection_strategy: str | None = None,
) -> CredentialParameter:
    now = datetime.now(UTC)
    return CredentialParameter(
        credential_parameter_id=f"cp_{key}",
        workflow_id="wf_test",
        key=key,
        credential_id=credential_id,
        credential_ids=credential_ids,
        selection_strategy=selection_strategy,
        created_at=now,
        modified_at=now,
    )


def _workflow_run(
    *,
    proxy_location: ProxyLocation | str | None = ProxyLocation.RESIDENTIAL_ISP,
    browser_profile_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        browser_profile_id=browser_profile_id,
        proxy_location=proxy_location,
    )


def _profile(
    *,
    proxy_session_id: str | None = None,
    is_managed: bool = True,
    browser_profile_id: str = "bp_managed",
) -> BrowserProfile:
    now = datetime.now(UTC)
    return BrowserProfile(
        browser_profile_id=browser_profile_id,
        organization_id="org_test",
        name="managed profile",
        proxy_session_id=proxy_session_id,
        proxy_location=ProxyLocation.RESIDENTIAL_ISP if proxy_session_id else None,
        is_managed=is_managed,
        created_at=now,
        modified_at=now,
    )


def _mock_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", AsyncMock(return_value=None))
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", AsyncMock())


async def _create_forced_workflow_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow: SimpleNamespace,
    workflow_request: WorkflowRequestBody | None = None,
    get_or_create_profile: AsyncMock | None = None,
    update_profile: AsyncMock | None = None,
    create_session: AsyncMock | None = None,
) -> SimpleNamespace:
    workflow_request = workflow_request or WorkflowRequestBody(proxy_location=ProxyLocation.RESIDENTIAL_ISP)
    get_or_create_profile = get_or_create_profile or AsyncMock(return_value=(_profile(), False))
    update_profile = update_profile or AsyncMock(return_value=_profile())
    create_events: list[str] = []
    created_workflow_run = SimpleNamespace(workflow_run_id="wr_forced")

    async def _create_workflow_run(**_: object) -> SimpleNamespace:
        create_events.append("create_workflow_run")
        return created_workflow_run

    create_workflow_run = AsyncMock(side_effect=_create_workflow_run)
    create_workflow_run.return_value = created_workflow_run
    if create_session is None:
        created_session = SimpleNamespace(persistent_browser_session_id="pbs_forced")

        async def _create_session(**_: object) -> SimpleNamespace:
            create_events.append("create_session")
            return created_session

        create_session = AsyncMock(side_effect=_create_session)
        create_session.return_value = created_session
    monkeypatch.setattr(
        app.EXPERIMENTATION_PROVIDER,
        "is_feature_enabled_cached",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_or_create_managed_browser_profile",
        get_or_create_profile,
    )
    monkeypatch.setattr(app.DATABASE.browser_sessions, "update_browser_profile", update_profile)
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "create_session", create_session)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_workflow_run", create_workflow_run)
    update_workflow_run = AsyncMock(
        return_value=SimpleNamespace(workflow_run_id="wr_forced", browser_session_id="pbs_forced")
    )
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update_workflow_run)
    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow", AsyncMock(return_value=workflow))

    result = await service.create_workflow_run(
        workflow_request=workflow_request,
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        organization_id="org_test",
    )

    if "create_session" in create_events:
        assert create_events.index("create_workflow_run") < create_events.index("create_session")

    return SimpleNamespace(
        result=result,
        create_session=create_session,
        get_or_create_profile=get_or_create_profile,
        update_profile=update_profile,
        create_workflow_run=create_workflow_run,
        update_workflow_run=update_workflow_run,
    )


async def _prepare_profile(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow: SimpleNamespace | None = None,
    workflow_run: SimpleNamespace | None = None,
    profile: SimpleNamespace | None = None,
    update_profile: AsyncMock | None = None,
    parameter_values: dict[str, object] | None = None,
) -> SimpleNamespace:
    workflow = workflow or _workflow()
    workflow_run = workflow_run or _workflow_run()
    profile = profile or _profile()
    update_profile = update_profile or AsyncMock(return_value=profile)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_or_create_managed_browser_profile",
        AsyncMock(return_value=(profile, False)),
    )
    monkeypatch.setattr(app.DATABASE.browser_sessions, "update_browser_profile", update_profile)
    updated_run = SimpleNamespace(**{**vars(workflow_run), "browser_profile_id": profile.browser_profile_id})
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", AsyncMock(return_value=updated_run))
    _mock_storage(monkeypatch)

    return await WorkflowService()._prepare_persisted_workflow_browser_profile(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run=workflow_run,  # type: ignore[arg-type]
        parameter_values=parameter_values or {"credential_id": "cred_a"},
    )


async def _setup_profile_with_reconcile_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow: SimpleNamespace,
    workflow_request: WorkflowRequestBody | None = None,
    profile: SimpleNamespace | None = None,
) -> tuple[WorkflowService, SimpleNamespace, AsyncMock, AsyncMock, Exception | None]:
    workflow_request = workflow_request or WorkflowRequestBody()
    profile = profile or _profile(proxy_session_id=None)
    workflow_run = _workflow_run(proxy_location=workflow_request.proxy_location or workflow.proxy_location)
    updated_run = SimpleNamespace(**{**vars(workflow_run), "browser_profile_id": profile.browser_profile_id})
    service = WorkflowService()
    service.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)  # type: ignore[method-assign]
    service.create_workflow_run = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service.get_workflow_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service.create_workflow_run_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service._select_rotating_credential_parameters_for_render = AsyncMock(return_value={})  # type: ignore[method-assign]
    service._record_workflow_run_metadata_in_background = MagicMock()  # type: ignore[method-assign]
    mark_failed = AsyncMock(return_value=workflow_run)
    service.mark_workflow_run_as_failed = mark_failed  # type: ignore[method-assign]
    update_profile = AsyncMock(side_effect=RuntimeError("db down"))
    outer_session = AsyncMock()

    monkeypatch.setattr(app.DATABASE.workflow_runs, "Session", lambda: MockAsyncSessionCtx(outer_session))
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_or_create_managed_browser_profile",
        AsyncMock(return_value=(profile, False)),
    )
    monkeypatch.setattr(app.DATABASE.browser_sessions, "update_browser_profile", update_profile)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", AsyncMock(return_value=updated_run))
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.skyvern_context.current", lambda: None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.skyvern_context.replace", MagicMock())
    _mock_storage(monkeypatch)

    caught: Exception | None = None
    try:
        result = await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=workflow_request,
            workflow_permanent_id="wpid_test",
            organization=SimpleNamespace(organization_id="org_test", organization_name="Test Org"),
        )
    except Exception as exc:
        caught = exc
        result = workflow_run
    return service, result, update_profile, mark_failed, caught


def test_derive_proxy_session_id_supports_profile_segments() -> None:
    proxy_session_id = derive_proxy_session_id("org_1", "wpid_1", "digest_a")

    assert len(proxy_session_id) == 10
    assert is_proxy_session_id(proxy_session_id)
    assert derive_proxy_session_id("org_1", "wpid_1", "digest_a") == proxy_session_id
    assert derive_proxy_session_id("org_1", "wpid_1", "digest_b") != proxy_session_id

    with pytest.raises(ValueError, match="empty parts"):
        derive_proxy_session_id("org_1", "wpid_1", "   ")
    with pytest.raises(ValueError, match="empty parts"):
        derive_proxy_session_id("org_1", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty parts"):
        derive_proxy_session_id()


@pytest.mark.asyncio
async def test_force_browser_session_passes_managed_profile_and_pins_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_profile = AsyncMock(return_value=_profile())
    workflow = _workflow(
        browser_profile_key="{{ credential_id }}",
        parameters=[_workflow_parameter("credential_id", default_value="cred_default")],
    )
    request = WorkflowRequestBody(
        data={"credential_id": "cred_request"},
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
    )

    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=workflow,
        workflow_request=request,
        update_profile=update_profile,
    )

    digest = build_browser_profile_key_digest("cred_request")
    expected_pin = derive_proxy_session_id("org_test", "wpid_test", digest)
    forced.get_or_create_profile.assert_awaited_once_with(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest=digest,
        name="Workflow (auto-saved: cred_request)",
    )
    update_profile.assert_awaited_once_with(
        profile_id="bp_managed",
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id=expected_pin,
    )
    forced.create_session.assert_awaited_once_with(
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        timeout_minutes=60,
        runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
        browser_profile_id="bp_managed",
        inherit_profile_proxy=True,
    )
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        browser_session_id="pbs_forced",
    )
    assert forced.result is forced.update_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_rotating_profile_key_selects_after_run_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_credential_for_run = AsyncMock(return_value="cred_selected")
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.service.select_credential_for_run",
        select_credential_for_run,
    )
    update_profile = AsyncMock(return_value=_profile())
    workflow = _workflow(
        browser_profile_key="{{ login_cred }}",
        parameters=[
            _credential_parameter(
                "login_cred",
                credential_ids=["cred_a", "cred_selected"],
                selection_strategy="round_robin",
            )
        ],
    )

    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=workflow,
        workflow_request=WorkflowRequestBody(
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        ),
        update_profile=update_profile,
    )

    digest = build_browser_profile_key_digest("cred_selected")
    select_credential_for_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        parameter_key="login_cred",
        credential_ids=["cred_a", "cred_selected"],
        selection_strategy="round_robin",
    )
    forced.get_or_create_profile.assert_awaited_once_with(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest=digest,
        name="Workflow (auto-saved: cred_selected)",
    )
    forced.create_session.assert_awaited_once_with(
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        timeout_minutes=60,
        runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
        browser_profile_id="bp_managed",
        inherit_profile_proxy=True,
    )
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        browser_session_id="pbs_forced",
    )
    assert forced.result is forced.update_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_rotating_profile_key_uses_run_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_credential_for_run = AsyncMock(return_value="cred_selected")
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.service.select_credential_for_run",
        select_credential_for_run,
    )
    update_profile = AsyncMock(return_value=_profile())
    workflow = _workflow(
        browser_profile_key="{{ login_cred }}",
        parameters=[
            _credential_parameter(
                "login_cred",
                credential_ids=["cred_a", "cred_selected"],
                selection_strategy="round_robin",
            )
        ],
    )

    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=workflow,
        workflow_request=WorkflowRequestBody(
            data={"login_cred": "cred_selected"},
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        ),
        update_profile=update_profile,
    )

    digest = build_browser_profile_key_digest("cred_selected")
    select_credential_for_run.assert_not_awaited()
    forced.get_or_create_profile.assert_awaited_once_with(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest=digest,
        name="Workflow (auto-saved: cred_selected)",
    )
    forced.create_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_browser_session_rotating_profile_key_selection_failure_returns_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_credential_for_run = AsyncMock(side_effect=RuntimeError("selection failed"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.service.select_credential_for_run",
        select_credential_for_run,
    )
    workflow = _workflow(
        browser_profile_key="{{ login_cred }}",
        parameters=[_credential_parameter("login_cred", credential_ids=["cred_a", "cred_b"])],
    )

    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=workflow,
        workflow_request=WorkflowRequestBody(proxy_location=ProxyLocation.RESIDENTIAL_ISP),
    )

    assert forced.result.workflow_run_id == "wr_forced"
    select_credential_for_run.assert_awaited_once()
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.get_or_create_profile.assert_not_awaited()
    forced.create_session.assert_not_awaited()
    forced.update_workflow_run.assert_not_awaited()
    assert forced.result is forced.create_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_created_profile_seeds_legacy_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    get_or_create_profile = AsyncMock(return_value=(profile, True))
    retrieve_browser_session = AsyncMock(return_value="/tmp/legacy-session")
    store_browser_profile = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", retrieve_browser_session)
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store_browser_profile)

    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(),
        get_or_create_profile=get_or_create_profile,
        update_profile=AsyncMock(return_value=profile),
    )

    retrieve_browser_session.assert_awaited_once_with("org_test", "wpid_test")
    store_browser_profile.assert_awaited_once_with(
        "org_test",
        profile_id="bp_managed",
        directory="/tmp/legacy-session",
    )
    forced.create_session.assert_awaited_once_with(
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        timeout_minutes=60,
        runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
        browser_profile_id="bp_managed",
        inherit_profile_proxy=True,
    )
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        browser_session_id="pbs_forced",
    )
    assert forced.result is forced.update_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_created_profile_seed_failure_rolls_back_and_skips_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    get_or_create_profile = AsyncMock(return_value=(profile, True))
    hard_delete = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", AsyncMock(return_value="/tmp/legacy-session"))
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", AsyncMock(side_effect=RuntimeError("storage down")))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "hard_delete_browser_profile", hard_delete)

    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(),
        get_or_create_profile=get_or_create_profile,
    )

    hard_delete.assert_awaited_once_with(profile_id="bp_managed", organization_id="org_test")
    forced.update_profile.assert_not_awaited()
    forced.create_session.assert_not_awaited()
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_not_awaited()
    assert forced.result is forced.create_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_existing_profile_does_not_seed_legacy_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve_browser_session = AsyncMock()
    store_browser_profile = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", retrieve_browser_session)
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store_browser_profile)

    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(),
        get_or_create_profile=AsyncMock(return_value=(_profile(), False)),
    )

    retrieve_browser_session.assert_not_awaited()
    store_browser_profile.assert_not_awaited()
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        browser_session_id="pbs_forced",
    )
    assert forced.result is forced.update_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_uses_workflow_proxy_default_for_profile_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_profile = AsyncMock(return_value=_profile())

    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(proxy_location=ProxyLocation.RESIDENTIAL_ISP),
        workflow_request=WorkflowRequestBody(),
        update_profile=update_profile,
    )

    expected_pin = derive_proxy_session_id("org_test", "wpid_test")
    update_profile.assert_awaited_once_with(
        profile_id="bp_managed",
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id=expected_pin,
    )
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        browser_session_id="pbs_forced",
    )
    assert forced.result is forced.update_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_persist_off_does_not_pass_browser_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(persist_browser_session=False),
    )

    forced.get_or_create_profile.assert_not_awaited()
    forced.update_profile.assert_not_awaited()
    forced.create_session.assert_awaited_once_with(
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        timeout_minutes=60,
        runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
        browser_profile_id=None,
        inherit_profile_proxy=True,
    )
    forced.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        browser_session_id="pbs_forced",
    )
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    assert forced.result is forced.update_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_pinned_unresolvable_profile_key_skips_forced_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        workflow_request=WorkflowRequestBody(data={}, proxy_location=ProxyLocation.RESIDENTIAL_ISP),
    )

    forced.get_or_create_profile.assert_not_awaited()
    forced.update_profile.assert_not_awaited()
    forced.create_session.assert_not_awaited()
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_not_awaited()
    assert forced.result is forced.create_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_pinned_profile_resolution_failure_skips_forced_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(),
        get_or_create_profile=AsyncMock(side_effect=RuntimeError("db down")),
    )

    forced.get_or_create_profile.assert_awaited_once()
    forced.update_profile.assert_not_awaited()
    forced.create_session.assert_not_awaited()
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_not_awaited()
    assert forced.result is forced.create_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_non_pinned_profile_resolution_failure_still_creates_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(pin_saved_session_ip=False),
        get_or_create_profile=AsyncMock(side_effect=RuntimeError("db down")),
    )

    forced.get_or_create_profile.assert_awaited_once()
    forced.update_profile.assert_not_awaited()
    forced.create_session.assert_awaited_once_with(
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        timeout_minutes=60,
        runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
        browser_profile_id=None,
        inherit_profile_proxy=True,
    )
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        browser_session_id="pbs_forced",
    )
    assert forced.result is forced.update_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_non_pinned_unresolvable_profile_key_creates_unprofiled_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}", pin_saved_session_ip=False),
        workflow_request=WorkflowRequestBody(data={}, proxy_location=ProxyLocation.RESIDENTIAL_ISP),
    )

    forced.get_or_create_profile.assert_not_awaited()
    forced.update_profile.assert_not_awaited()
    forced.create_session.assert_awaited_once_with(
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        timeout_minutes=60,
        runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
        browser_profile_id=None,
        inherit_profile_proxy=True,
    )
    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_forced",
        browser_session_id="pbs_forced",
    )
    assert forced.result is forced.update_workflow_run.return_value


@pytest.mark.asyncio
async def test_force_browser_session_creation_failure_returns_run_without_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced = await _create_forced_workflow_run(
        monkeypatch,
        workflow=_workflow(),
        create_session=AsyncMock(side_effect=RuntimeError("session creation failed")),
    )

    forced.create_workflow_run.assert_awaited_once()
    assert forced.create_workflow_run.await_args.kwargs["browser_session_id"] is None
    forced.create_session.assert_awaited_once()
    forced.update_workflow_run.assert_not_awaited()
    assert forced.result is forced.create_workflow_run.return_value


@pytest.mark.asyncio
async def test_create_workflow_run_non_force_path_single_create_no_update(monkeypatch: pytest.MonkeyPatch) -> None:
    request = WorkflowRequestBody(
        browser_session_id="pbs_requested",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
    )
    create_workflow_run = AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr_non_force"))
    update_workflow_run = AsyncMock()
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_persistent_browser_session",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id=None)),
    )
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_workflow_run", create_workflow_run)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update_workflow_run)

    result = await WorkflowService().create_workflow_run(
        workflow_request=request,
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        organization_id="org_test",
    )

    assert result.workflow_run_id == "wr_non_force"
    create_workflow_run.assert_awaited_once_with(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        organization_id="org_test",
        browser_session_id="pbs_requested",
        browser_profile_id=None,
        proxy_location=request.proxy_location,
        webhook_callback_url=request.webhook_callback_url,
        totp_verification_url=request.totp_verification_url,
        totp_identifier=request.totp_identifier,
        parent_workflow_run_id=None,
        max_screenshot_scrolling_times=request.max_screenshot_scrolls,
        max_elapsed_time_minutes=None,
        extra_http_headers=request.extra_http_headers,
        cdp_connect_headers=request.cdp_connect_headers,
        browser_address=request.browser_address,
        sequential_key=None,
        run_with=request.run_with,
        debug_session_id=None,
        ai_fallback=request.ai_fallback,
        code_gen=None,
        workflow_run_id=None,
        trigger_type=None,
        workflow_schedule_id=None,
        retried_from_workflow_run_id=None,
        fallback_attempt=None,
        ignore_inherited_workflow_system_prompt=False,
        copilot_session_id=None,
    )
    update_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_managed_profile_sets_deterministic_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_profile = AsyncMock(return_value=_profile())
    digest = build_browser_profile_key_digest("cred_a")

    result = await _prepare_profile(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        profile=_profile(proxy_session_id=None),
        update_profile=update_profile,
    )

    expected_pin = derive_proxy_session_id("org_test", "wpid_test", digest)
    assert result.browser_profile_id == "bp_managed"
    update_profile.assert_awaited_once_with(
        profile_id="bp_managed",
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id=expected_pin,
    )


@pytest.mark.asyncio
async def test_prepare_managed_profile_pins_keyless_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    update_profile = AsyncMock(return_value=_profile())

    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(browser_profile_key=None),
        profile=_profile(proxy_session_id=None),
        update_profile=update_profile,
    )

    update_profile.assert_awaited_once_with(
        profile_id="bp_managed",
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id=derive_proxy_session_id("org_test", "wpid_test"),
    )


@pytest.mark.asyncio
async def test_prepare_managed_profile_keeps_correct_existing_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    update_profile = AsyncMock()
    expected_pin = derive_proxy_session_id("org_test", "wpid_test", build_browser_profile_key_digest("cred_a"))

    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        profile=_profile(proxy_session_id=expected_pin),
        update_profile=update_profile,
    )

    update_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_managed_profile_heals_stale_proxy_location(monkeypatch: pytest.MonkeyPatch) -> None:
    update_profile = AsyncMock(return_value=_profile())
    expected_pin = derive_proxy_session_id("org_test", "wpid_test", build_browser_profile_key_digest("cred_a"))
    profile = _profile(proxy_session_id=expected_pin)
    profile.proxy_location = None

    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        profile=profile,
        update_profile=update_profile,
    )

    update_profile.assert_awaited_once_with(
        profile_id="bp_managed",
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id=expected_pin,
    )


@pytest.mark.asyncio
async def test_prepare_managed_profile_heals_drifted_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    update_profile = AsyncMock(return_value=_profile())
    expected_pin = derive_proxy_session_id("org_test", "wpid_test", build_browser_profile_key_digest("cred_a"))

    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        profile=_profile(proxy_session_id="deadbeef99"),
        update_profile=update_profile,
    )

    update_profile.assert_awaited_once_with(
        profile_id="bp_managed",
        organization_id="org_test",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id=expected_pin,
    )


@pytest.mark.asyncio
async def test_prepare_managed_profile_clears_pin_when_toggle_off(monkeypatch: pytest.MonkeyPatch) -> None:
    update_profile = AsyncMock(return_value=_profile(proxy_session_id=None))

    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(pin_saved_session_ip=False),
        profile=_profile(proxy_session_id="abc1234567"),
        update_profile=update_profile,
    )

    update_profile.assert_awaited_once_with(
        profile_id="bp_managed",
        organization_id="org_test",
        proxy_location=None,
        proxy_session_id=None,
    )


@pytest.mark.asyncio
async def test_prepare_managed_profile_clears_pin_for_non_isp_proxy_location(monkeypatch: pytest.MonkeyPatch) -> None:
    update_profile = AsyncMock(return_value=_profile(proxy_session_id=None))

    await _prepare_profile(
        monkeypatch,
        workflow_run=_workflow_run(proxy_location=ProxyLocation.RESIDENTIAL),
        profile=_profile(proxy_session_id="abc1234567"),
        update_profile=update_profile,
    )

    update_profile.assert_awaited_once_with(
        profile_id="bp_managed",
        organization_id="org_test",
        proxy_location=None,
        proxy_session_id=None,
    )


@pytest.mark.asyncio
async def test_prepare_managed_profile_does_not_set_pin_for_non_isp_proxy_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_profile = AsyncMock()

    await _prepare_profile(
        monkeypatch,
        workflow_run=_workflow_run(proxy_location=None),
        profile=_profile(proxy_session_id=None),
        update_profile=update_profile,
    )

    update_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_pinned_isp_managed_profile_reconcile_failure_fails_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result, update_profile, mark_failed, caught = await _setup_profile_with_reconcile_failure(
        monkeypatch,
        workflow=_workflow(),
        workflow_request=WorkflowRequestBody(proxy_location=ProxyLocation.RESIDENTIAL_ISP),
    )

    assert isinstance(caught, RuntimeError)
    assert str(caught) == "db down"
    assert result.browser_profile_id is None
    update_profile.assert_awaited_once()
    mark_failed.assert_awaited_once()
    assert mark_failed.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert mark_failed.await_args.kwargs["failure_reason"].startswith("Setup workflow failed. failure reason:")


@pytest.mark.asyncio
async def test_setup_non_pinned_reconcile_failure_still_stamps_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result, update_profile, mark_failed, caught = await _setup_profile_with_reconcile_failure(
        monkeypatch,
        workflow=_workflow(pin_saved_session_ip=False),
        profile=_profile(proxy_session_id="abc1234567"),
    )

    assert caught is None
    assert result.browser_profile_id == "bp_managed"
    update_profile.assert_awaited_once()
    mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_pinned_non_isp_reconcile_failure_still_stamps_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result, update_profile, mark_failed, caught = await _setup_profile_with_reconcile_failure(
        monkeypatch,
        workflow=_workflow(proxy_location=ProxyLocation.RESIDENTIAL),
        workflow_request=WorkflowRequestBody(proxy_location=ProxyLocation.RESIDENTIAL),
        profile=_profile(proxy_session_id="abc1234567"),
    )

    assert caught is None
    assert result.browser_profile_id == "bp_managed"
    update_profile.assert_awaited_once()
    mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_user_profile_pin_is_never_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    update_profile = AsyncMock()

    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(pin_saved_session_ip=False),
        profile=_profile(proxy_session_id="abc1234567", is_managed=False),
        update_profile=update_profile,
    )

    update_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_managed_profile_pin_is_deterministic_by_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[str] = []

    async def _record_update(**kwargs: object) -> SimpleNamespace:
        updates.append(str(kwargs["proxy_session_id"]))
        return _profile(proxy_session_id=str(kwargs["proxy_session_id"]))

    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        profile=_profile(proxy_session_id=None, browser_profile_id="bp_first"),
        update_profile=AsyncMock(side_effect=_record_update),
    )
    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        profile=_profile(proxy_session_id=None, browser_profile_id="bp_second"),
        update_profile=AsyncMock(side_effect=_record_update),
    )
    await _prepare_profile(
        monkeypatch,
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        profile=_profile(proxy_session_id=None, browser_profile_id="bp_third"),
        update_profile=AsyncMock(side_effect=_record_update),
        parameter_values={"credential_id": "cred_b"},
    )

    same_segment_pin = derive_proxy_session_id("org_test", "wpid_test", build_browser_profile_key_digest("cred_a"))
    different_segment_pin = derive_proxy_session_id(
        "org_test",
        "wpid_test",
        build_browser_profile_key_digest("cred_b"),
    )
    assert updates == [same_segment_pin, same_segment_pin, different_segment_pin]
    assert different_segment_pin != same_segment_pin


class _SessionScalars:
    def __init__(self, profile: SimpleNamespace | None) -> None:
        self._profile = profile

    def first(self) -> SimpleNamespace | None:
        return self._profile


async def _create_repo_session(
    *,
    profile: SimpleNamespace | None,
    proxy_location: ProxyLocation | str | None = ProxyLocation.RESIDENTIAL_ISP,
    proxy_session_id: str | None = None,
    browser_profile_id: str | None = "bp_managed",
    inherit_profile_proxy: bool = False,
    expect_profile_lookup: bool | None = None,
) -> SimpleNamespace:
    mock_session = AsyncMock()
    mock_session.scalars.return_value = _SessionScalars(profile)
    mock_session.add = MagicMock()

    async def _flush() -> None:
        stored_session = mock_session.add.call_args.args[0]
        if getattr(stored_session, "persistent_browser_session_id", None) is None:
            stored_session.persistent_browser_session_id = "pbs_test"

    mock_session.flush = AsyncMock(side_effect=_flush)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    repo = BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))

    def _validate(model: object) -> SimpleNamespace:
        return SimpleNamespace(
            persistent_browser_session_id=model.persistent_browser_session_id,
            proxy_location=model.proxy_location,
            proxy_session_id=model.proxy_session_id,
            browser_profile_id=model.browser_profile_id,
        )

    with patch(
        "skyvern.forge.sdk.schemas.persistent_browser_sessions.PersistentBrowserSession.model_validate",
        side_effect=_validate,
    ):
        created_session = await repo.create_persistent_browser_session(
            organization_id="org_test",
            proxy_location=proxy_location,
            proxy_session_id=proxy_session_id,
            browser_profile_id=browser_profile_id,
            inherit_profile_proxy=inherit_profile_proxy,
        )
    if expect_profile_lookup is True:
        mock_session.scalars.assert_awaited_once()
    elif expect_profile_lookup is False:
        mock_session.scalars.assert_not_awaited()
    return created_session


@pytest.mark.asyncio
async def test_create_persistent_browser_session_inherits_profile_pin() -> None:
    # The ORM row exposes proxy_location as a serialized string; the repo must deserialize it
    # before it reaches serialize_proxy_location (which rejects a bare str).
    profile = SimpleNamespace(
        browser_profile_id="bp_managed",
        proxy_session_id="abc1234567",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP.value,
        is_managed=True,
    )
    session = await _create_repo_session(profile=profile, inherit_profile_proxy=True)

    assert session.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert session.proxy_session_id == "abc1234567"
    assert session.browser_profile_id == "bp_managed"


@pytest.mark.asyncio
async def test_create_persistent_browser_session_explicit_pin_wins() -> None:
    session = await _create_repo_session(
        profile=_profile(proxy_session_id="abc1234567"),
        proxy_session_id="fff1234567",
        inherit_profile_proxy=True,
    )

    assert session.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert session.proxy_session_id == "fff1234567"


@pytest.mark.asyncio
async def test_create_persistent_browser_session_pinless_profile_keeps_auto_generate_behavior() -> None:
    session = await _create_repo_session(profile=_profile(proxy_session_id=None), inherit_profile_proxy=True)

    assert session.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert is_proxy_session_id(session.proxy_session_id)
    assert session.proxy_session_id != "abc1234567"


@pytest.mark.asyncio
async def test_create_persistent_browser_session_does_not_inherit_profile_pin_by_default() -> None:
    profile = _profile(proxy_session_id="abc1234567")

    session = await _create_repo_session(
        profile=profile,
        proxy_location=None,
        browser_profile_id="bp_managed",
        expect_profile_lookup=False,
    )

    assert session.proxy_location is None
    assert session.proxy_session_id is None
    assert session.browser_profile_id == "bp_managed"
