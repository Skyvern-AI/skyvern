from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import InvalidCredentialId, SkyvernHTTPException
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.credential_fallback import (
    ANY_FAILURE,
    CREDENTIAL_FAILURES,
    _trigger_matches,
    maybe_start_credential_fallback_retry,
)
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import CredentialParameterYAML, WorkflowDefinitionYAML


def _credential_parameter(
    *,
    key: str = "login_cred",
    credential_id: str = "cred_primary",
    credential_ids: list[str] | None = None,
    fallback_credential_ids: list[str] | None = None,
    fallback_trigger: str | None = None,
) -> CredentialParameter:
    now = datetime.now(timezone.utc)
    return CredentialParameter(
        key=key,
        credential_parameter_id=f"cp_{key}",
        workflow_id="wf_test",
        credential_id=credential_id,
        credential_ids=credential_ids,
        fallback_credential_ids=fallback_credential_ids,
        fallback_trigger=fallback_trigger,
        created_at=now,
        modified_at=now,
    )


def _workflow_run(
    *,
    workflow_run_id: str = "wr_failed",
    status: WorkflowRunStatus = WorkflowRunStatus.failed,
    failure_category: list[dict] | None = None,
    failure_reason: str | None = "login failed",
    fallback_attempt: int | None = None,
    parent_workflow_run_id: str | None = None,
    debug_session_id: str | None = None,
    retried_from_workflow_run_id: str | None = None,
    browser_session_id: str | None = None,
    browser_profile_id: str | None = None,
    extra_http_headers: dict[str, str] | None = None,
    workflow_schedule_id: str | None = None,
) -> WorkflowRun:
    now = datetime.now(timezone.utc)
    return WorkflowRun(
        workflow_run_id=workflow_run_id,
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        status=status,
        failure_reason=failure_reason,
        failure_category=failure_category,
        fallback_attempt=fallback_attempt,
        parent_workflow_run_id=parent_workflow_run_id,
        debug_session_id=debug_session_id,
        retried_from_workflow_run_id=retried_from_workflow_run_id,
        browser_session_id=browser_session_id,
        browser_profile_id=browser_profile_id,
        extra_http_headers=extra_http_headers,
        workflow_schedule_id=workflow_schedule_id,
        trigger_type=WorkflowRunTriggerType.api,
        created_at=now,
        modified_at=now,
    )


def _workflow(parameters: list[CredentialParameter]) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        version=7,
        title="Workflow",
        workflow_definition=SimpleNamespace(parameters=parameters),
    )


async def _validate_parameter(parameter: CredentialParameter, existing_ids: list[str] | None = None) -> None:
    service = WorkflowService()
    organization = SimpleNamespace(organization_id="org_test")
    existing = [SimpleNamespace(credential_id=credential_id) for credential_id in existing_ids or []]
    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(return_value=existing)
        await service._validate_and_normalize_credential_rotation_parameters([parameter], organization)


@pytest.mark.asyncio
async def test_workflow_save_validation_dedupes_fallback_credentials_preserving_order() -> None:
    parameter = _credential_parameter(
        fallback_credential_ids=["cred_b", "cred_c", "cred_b", "cred_d", "cred_c"],
    )

    await _validate_parameter(parameter, ["cred_b", "cred_c", "cred_d"])

    assert parameter.fallback_credential_ids == ["cred_b", "cred_c", "cred_d"]
    assert parameter.credential_id == "cred_primary"


@pytest.mark.asyncio
async def test_workflow_save_validation_rejects_unknown_fallback_credential_id() -> None:
    parameter = _credential_parameter(fallback_credential_ids=["cred_missing"])

    with pytest.raises(InvalidCredentialId):
        await _validate_parameter(parameter, [])


@pytest.mark.asyncio
async def test_workflow_save_validation_rejects_bad_fallback_trigger() -> None:
    parameter = _credential_parameter(fallback_credential_ids=["cred_b"], fallback_trigger="later")

    with pytest.raises(SkyvernHTTPException, match="fallback_trigger"):
        await _validate_parameter(parameter, ["cred_b"])


@pytest.mark.asyncio
async def test_workflow_save_validation_rejects_trigger_without_fallback_list() -> None:
    parameter = _credential_parameter(fallback_trigger=ANY_FAILURE)

    with pytest.raises(SkyvernHTTPException, match="requires fallback_credential_ids"):
        await _validate_parameter(parameter)


@pytest.mark.asyncio
async def test_workflow_save_validation_normalizes_empty_fallback_list_to_none() -> None:
    parameter = _credential_parameter(fallback_credential_ids=[])

    await _validate_parameter(parameter)

    assert parameter.fallback_credential_ids is None


@pytest.mark.asyncio
async def test_workflow_save_validation_drops_primary_credential_from_fallbacks() -> None:
    parameter = _credential_parameter(fallback_credential_ids=["cred_primary", "cred_b"])

    await _validate_parameter(parameter, ["cred_b"])

    assert parameter.fallback_credential_ids == ["cred_b"]


@pytest.mark.asyncio
async def test_workflow_save_validation_rejects_trigger_when_fallbacks_collapse_to_primary() -> None:
    parameter = _credential_parameter(fallback_credential_ids=["cred_primary"], fallback_trigger=ANY_FAILURE)

    with pytest.raises(SkyvernHTTPException, match="requires fallback_credential_ids"):
        await _validate_parameter(parameter)


@pytest.mark.asyncio
async def test_workflow_save_validation_rejects_rotation_with_fallback_credentials() -> None:
    parameter = _credential_parameter(
        credential_ids=["cred_primary", "cred_alt"],
        fallback_credential_ids=["cred_primary", "cred_b"],
    )

    with pytest.raises(SkyvernHTTPException) as exc_info:
        await _validate_parameter(parameter, ["cred_primary", "cred_alt", "cred_b"])

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == (
        "credential parameter login_cred cannot combine credential_ids rotation with fallback_credential_ids; "
        "configure one or the other."
    )


@pytest.mark.asyncio
async def test_workflow_save_validation_allows_rotation_without_fallback_credentials() -> None:
    parameter = _credential_parameter(credential_ids=["cred_primary", "cred_alt"])

    await _validate_parameter(parameter, ["cred_primary", "cred_alt"])

    assert parameter.credential_ids == ["cred_primary", "cred_alt"]
    assert parameter.credential_id == "cred_primary"


@pytest.mark.asyncio
async def test_workflow_save_validation_allows_fallback_credentials_without_rotation() -> None:
    parameter = _credential_parameter(fallback_credential_ids=["cred_b", "cred_c"])

    await _validate_parameter(parameter, ["cred_b", "cred_c"])

    assert parameter.credential_ids is None
    assert parameter.fallback_credential_ids == ["cred_b", "cred_c"]


@pytest.mark.parametrize("category", ["AUTH_FAILURE", "CREDENTIAL_ERROR"])
def test_credential_failure_trigger_matches_credential_categories(category: str) -> None:
    workflow_run = _workflow_run(failure_category=[{"category": category}])

    assert _trigger_matches(workflow_run, CREDENTIAL_FAILURES)


def test_credential_failure_trigger_ignores_noncredential_categories() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "ELEMENT_NOT_FOUND"}])

    assert not _trigger_matches(workflow_run, CREDENTIAL_FAILURES)


def test_credential_failure_trigger_falls_back_to_failure_reason_classification() -> None:
    workflow_run = _workflow_run(failure_category=None, failure_reason="The password was incorrect during login")

    assert _trigger_matches(workflow_run, CREDENTIAL_FAILURES)


@pytest.mark.parametrize("status", [WorkflowRunStatus.failed, WorkflowRunStatus.terminated])
def test_any_failure_trigger_matches_failed_and_terminated_runs(status: WorkflowRunStatus) -> None:
    workflow_run = _workflow_run(status=status, failure_category=[{"category": "ELEMENT_NOT_FOUND"}])

    assert _trigger_matches(workflow_run, ANY_FAILURE)


PROXY_SESSION_MARKER_HEADER = "x-proxy-session-marker"


def _strip_marker_header(extra_http_headers: dict[str, str] | None) -> dict[str, str] | None:
    if not extra_http_headers:
        return extra_http_headers
    return {key: value for key, value in extra_http_headers.items() if key != PROXY_SESSION_MARKER_HEADER} or None


async def _run_fallback_retry(
    *,
    workflow_run: WorkflowRun,
    parameters: list[CredentialParameter],
    existing_retry_run_id: str | None = None,
    retried_by_results: list[str | None] | None = None,
    prior_selections: dict[str, str] | None = None,
    block_scoped: bool = False,
    missing_credential_ids: set[str] | None = None,
    run_workflow_error: Exception | None = None,
    flag_enabled: bool = True,
    flag_error: Exception | None = None,
) -> tuple[str | None, AsyncMock, MagicMock]:
    captured: dict[str, object] = {}

    async def fake_run_workflow(**kwargs: object) -> WorkflowRun:
        captured["context_at_call"] = skyvern_context.current()
        if run_workflow_error is not None:
            raise run_workflow_error
        return _workflow_run(
            workflow_run_id="wr_retry",
            status=WorkflowRunStatus.created,
            failure_category=None,
            failure_reason=None,
            retried_from_workflow_run_id=kwargs["retried_from_workflow_run_id"],  # type: ignore[arg-type]
            fallback_attempt=kwargs["fallback_attempt"],  # type: ignore[arg-type]
        )

    run_workflow_mock = AsyncMock(side_effect=fake_run_workflow)
    run_workflow_mock.captured = captured
    with (
        patch("skyvern.forge.sdk.workflow.credential_fallback.app") as mock_app,
        patch("skyvern.services.workflow_service.run_workflow", run_workflow_mock),
    ):
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(
            side_effect=flag_error, return_value=flag_enabled
        )
        mock_app.AGENT_FUNCTION.is_block_scoped_workflow_run = AsyncMock(return_value=block_scoped)
        mock_app.AGENT_FUNCTION.strip_proxy_session_extra_http_headers = _strip_marker_header
        mock_app.DATABASE.debug.has_block_run_for_workflow_run = AsyncMock(return_value=False)
        mock_app.WORKFLOW_SERVICE.get_workflow = AsyncMock(return_value=_workflow(parameters))
        if retried_by_results is not None:
            mock_app.DATABASE.workflow_runs.get_workflow_run_retried_by = AsyncMock(side_effect=retried_by_results)
        else:
            mock_app.DATABASE.workflow_runs.get_workflow_run_retried_by = AsyncMock(return_value=existing_retry_run_id)
        mock_app.DATABASE.workflow_run_credential_selections.get_selections_for_run = AsyncMock(
            return_value=prior_selections or {}
        )

        async def fake_get_credentials_by_ids(credential_ids: list[str], organization_id: str) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(credential_id=credential_id)
                for credential_id in credential_ids
                if credential_id not in (missing_credential_ids or set())
            ]

        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(side_effect=fake_get_credentials_by_ids)
        mock_app.DATABASE.workflow_runs.get_workflow_run_parameters = AsyncMock(
            return_value=[
                (
                    SimpleNamespace(key="account_id"),
                    SimpleNamespace(value="acct_1"),
                )
            ]
        )
        mock_app.DATABASE.organizations.get_organization = AsyncMock(
            return_value=SimpleNamespace(organization_id="org_test")
        )
        result = await maybe_start_credential_fallback_retry(workflow_run, "org_test")
        return result, run_workflow_mock, mock_app


@pytest.mark.asyncio
async def test_fallback_attempt_one_uses_first_fallback_and_preserves_other_pins() -> None:
    workflow_run = _workflow_run(
        failure_category=[{"category": "AUTH_FAILURE"}],
        browser_session_id="pbs_live",
        browser_profile_id="bprof_old",
        workflow_schedule_id="ws_1",
    )
    login = _credential_parameter(fallback_credential_ids=["cred_fb1", "cred_fb2"])
    backup = _credential_parameter(
        key="backup_cred",
        credential_id="cred_backup_primary",
        credential_ids=["cred_backup_primary", "cred_backup_alt"],
    )

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login, backup],
        prior_selections={"backup_cred": "cred_backup_alt", "login_cred": "cred_primary"},
    )

    assert result == "wr_retry"
    kwargs = run_workflow_mock.await_args.kwargs
    assert kwargs["retried_from_workflow_run_id"] == "wr_failed"
    assert kwargs["fallback_attempt"] == 1
    assert kwargs["version"] == 7
    assert kwargs["trigger_type"] == WorkflowRunTriggerType.api
    assert kwargs["workflow_schedule_id"] == "ws_1"
    assert kwargs["workflow_request"].browser_session_id is None
    assert kwargs["workflow_request"].browser_profile_id is None
    assert kwargs["workflow_request"].data == {
        "account_id": "acct_1",
        "login_cred": "cred_fb1",
        "backup_cred": "cred_backup_alt",
    }


@pytest.mark.asyncio
async def test_fallback_attempt_two_advances_from_parameter_prior_selection() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}], fallback_attempt=1)
    login = _credential_parameter(fallback_credential_ids=["cred_fb1", "cred_fb2"])

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        prior_selections={"login_cred": "cred_fb1"},
    )

    assert result == "wr_retry"
    assert run_workflow_mock.await_args.kwargs["fallback_attempt"] == 2
    assert run_workflow_mock.await_args.kwargs["workflow_request"].data["login_cred"] == "cred_fb2"


@pytest.mark.asyncio
async def test_mixed_triggers_advance_each_parameter_from_its_own_prior_selection() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}], fallback_attempt=1)
    any_failure_param = _credential_parameter(
        key="login_cred",
        fallback_credential_ids=["cred_a_fb1", "cred_a_fb2"],
        fallback_trigger=ANY_FAILURE,
    )
    credential_failure_param = _credential_parameter(
        key="backup_cred",
        credential_id="cred_backup_primary",
        fallback_credential_ids=["cred_b_fb1", "cred_b_fb2"],
        fallback_trigger=CREDENTIAL_FAILURES,
    )

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[any_failure_param, credential_failure_param],
        prior_selections={"login_cred": "cred_a_fb1"},
    )

    assert result == "wr_retry"
    kwargs = run_workflow_mock.await_args.kwargs
    assert kwargs["fallback_attempt"] == 2
    assert kwargs["workflow_request"].data["login_cred"] == "cred_a_fb2"
    assert kwargs["workflow_request"].data["backup_cred"] == "cred_b_fb1"


@pytest.mark.asyncio
async def test_fallback_exhaustion_creates_no_retry_run() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}], fallback_attempt=2)
    login = _credential_parameter(fallback_credential_ids=["cred_fb1", "cred_fb2"])

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        prior_selections={"login_cred": "cred_fb2"},
    )

    assert result is None
    run_workflow_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_disabled_creates_no_retry_run_and_reads_nothing() -> None:
    """Default-off gate: a run that would otherwise retry must do nothing at all.

    No retry run means no credits consumed and no webhook re-fired.
    """
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(fallback_credential_ids=["cred_fb1", "cred_fb2"])

    result, run_workflow_mock, mock_app = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        flag_enabled=False,
    )

    assert result is None
    run_workflow_mock.assert_not_awaited()
    mock_app.WORKFLOW_SERVICE.get_workflow.assert_not_called()
    mock_app.DATABASE.workflow_runs.get_workflow_run_retried_by.assert_not_called()


@pytest.mark.asyncio
async def test_flag_evaluation_failure_fails_closed() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(fallback_credential_ids=["cred_fb1", "cred_fb2"])

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        flag_error=RuntimeError("posthog is down"),
    )

    assert result is None
    run_workflow_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_fallback_credential_is_skipped_in_favor_of_next() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(fallback_credential_ids=["cred_fb1", "cred_fb2"])

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        missing_credential_ids={"cred_fb1"},
    )

    assert result == "wr_retry"
    kwargs = run_workflow_mock.await_args.kwargs
    assert kwargs["fallback_attempt"] == 1
    assert kwargs["workflow_request"].data["login_cred"] == "cred_fb2"


@pytest.mark.asyncio
async def test_all_fallback_credentials_deleted_does_not_advance() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(fallback_credential_ids=["cred_fb1", "cred_fb2"])

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        missing_credential_ids={"cred_fb1", "cred_fb2"},
    )

    assert result is None
    run_workflow_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotation_selected_prior_in_fallback_list_does_not_fake_progress() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(
        credential_id="cred_a",
        credential_ids=["cred_a", "cred_b"],
        fallback_credential_ids=["cred_c", "cred_b"],
    )

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        prior_selections={"login_cred": "cred_b"},
    )

    assert result == "wr_retry"
    kwargs = run_workflow_mock.await_args.kwargs
    assert kwargs["fallback_attempt"] == 1
    assert kwargs["workflow_request"].data["login_cred"] == "cred_c"


@pytest.mark.asyncio
async def test_retry_request_strips_credential_proxy_session_headers() -> None:
    workflow_run = _workflow_run(
        failure_category=[{"category": "AUTH_FAILURE"}],
        extra_http_headers={PROXY_SESSION_MARKER_HEADER: "proxy_sess_1", "x-custom": "keep"},
    )
    login = _credential_parameter(fallback_credential_ids=["cred_fb1"])

    result, run_workflow_mock, _ = await _run_fallback_retry(workflow_run=workflow_run, parameters=[login])

    assert result == "wr_retry"
    assert run_workflow_mock.await_args.kwargs["workflow_request"].extra_http_headers == {"x-custom": "keep"}


@pytest.mark.asyncio
async def test_retry_run_creation_does_not_inherit_ambient_skyvern_context() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(fallback_credential_ids=["cred_fb1"])
    ambient = SkyvernContext(run_id="wr_failed", workflow_run_id="wr_failed", root_workflow_run_id="wr_failed")
    skyvern_context.set(ambient)
    try:
        result, run_workflow_mock, _ = await _run_fallback_retry(workflow_run=workflow_run, parameters=[login])
        assert skyvern_context.current() is ambient
    finally:
        skyvern_context.reset()

    assert result == "wr_retry"
    context_at_call = run_workflow_mock.captured["context_at_call"]
    assert context_at_call is None or (context_at_call.run_id is None and context_at_call.root_workflow_run_id is None)


@pytest.mark.asyncio
async def test_duplicate_retry_integrity_error_recovers_existing_retry_run() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(fallback_credential_ids=["cred_fb1"])
    duplicate_error = IntegrityError(
        "INSERT INTO workflow_runs",
        None,
        Exception('duplicate key value violates unique constraint "ix_workflow_runs_retried_from_workflow_run_id"'),
    )

    result, _, mock_app = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        retried_by_results=[None, "wr_existing"],
        run_workflow_error=duplicate_error,
    )

    assert result == "wr_existing"
    assert mock_app.DATABASE.workflow_runs.get_workflow_run_retried_by.await_count == 2


@pytest.mark.asyncio
async def test_unrelated_integrity_error_is_not_treated_as_duplicate_retry() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(fallback_credential_ids=["cred_fb1"])
    unrelated_error = IntegrityError(
        "INSERT INTO workflow_runs",
        None,
        Exception('null value in column "organization_id" violates not-null constraint'),
    )

    result, _, mock_app = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        retried_by_results=[None, "wr_existing"],
        run_workflow_error=unrelated_error,
    )

    assert result is None
    assert mock_app.DATABASE.workflow_runs.get_workflow_run_retried_by.await_count == 1


@pytest.mark.asyncio
async def test_fallback_retry_idempotency_returns_existing_retry_run() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    login = _credential_parameter(fallback_credential_ids=["cred_fb1"])

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        existing_retry_run_id="wr_existing",
    )

    assert result == "wr_existing"
    run_workflow_mock.assert_not_awaited()


@pytest.mark.parametrize(
    ("workflow_run", "block_scoped"),
    [
        (_workflow_run(parent_workflow_run_id="wr_parent"), False),
        (_workflow_run(debug_session_id="debug_1"), False),
        (_workflow_run(), True),
    ],
)
@pytest.mark.asyncio
async def test_fallback_retry_guards_skip_nested_debug_and_block_scoped_runs(
    workflow_run: WorkflowRun,
    block_scoped: bool,
) -> None:
    login = _credential_parameter(fallback_credential_ids=["cred_fb1"])

    result, run_workflow_mock, _ = await _run_fallback_retry(
        workflow_run=workflow_run,
        parameters=[login],
        block_scoped=block_scoped,
    )

    assert result is None
    run_workflow_mock.assert_not_awaited()


def test_credential_override_accepts_fallback_pool_and_rejects_non_pool_id() -> None:
    service = WorkflowService()
    workflow = _workflow([_credential_parameter(fallback_credential_ids=["cred_fb1"])])

    overrides = service._get_run_credential_parameter_overrides(
        workflow=workflow,
        request_data={"login_cred": "cred_fb1"},
    )

    assert overrides == {"login_cred": "cred_fb1"}
    with pytest.raises(SkyvernHTTPException, match="configured rotation or fallback credentials"):
        service._get_run_credential_parameter_overrides(
            workflow=workflow,
            request_data={"login_cred": "cred_other"},
        )


@pytest.mark.asyncio
async def test_service_resolves_fallback_only_credential_parameter_from_pinned_selection() -> None:
    service = WorkflowService()
    parameter = _credential_parameter(fallback_credential_ids=["cred_fb1"])

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts = {}
        mock_app.DATABASE.workflow_run_credential_selections.get_selection = AsyncMock(return_value="cred_fb1")
        selected = await service._resolve_credential_parameter_id(
            parameter=parameter,
            workflow_run_id="wr_retry",
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
        )

    assert selected == "cred_fb1"


@pytest.mark.asyncio
async def test_context_resolves_fallback_only_credential_parameter_from_pinned_selection_once() -> None:
    context = WorkflowRunContext(
        workflow_title="Workflow",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_retry",
        aws_client=MagicMock(),
    )
    parameter = _credential_parameter(fallback_credential_ids=["cred_fb1"])

    with patch("skyvern.forge.sdk.workflow.context_manager.app") as mock_app:
        get_selection = AsyncMock(return_value="cred_fb1")
        mock_app.DATABASE.workflow_run_credential_selections.get_selection = get_selection
        first = await context.resolve_credential_parameter_id(parameter, "org_test")
        second = await context.resolve_credential_parameter_id(parameter, "org_test")

    assert first == "cred_fb1"
    assert second == "cred_fb1"
    get_selection.assert_awaited_once_with(workflow_run_id="wr_retry", parameter_key="login_cred")


@pytest.mark.parametrize(
    ("pinned_selection", "expected"),
    [
        (None, "cred_runtime"),
        ("cred_fb1", "cred_fb1"),
    ],
)
@pytest.mark.asyncio
async def test_context_fallback_only_parameter_applies_credential_id_indirection(
    pinned_selection: str | None,
    expected: str,
) -> None:
    context = WorkflowRunContext(
        workflow_title="Workflow",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_retry",
        aws_client=MagicMock(),
    )
    context.parameters["credential_source"] = MagicMock()
    context.values["credential_source"] = "cred_runtime"
    parameter = _credential_parameter(
        credential_id="credential_source",
        fallback_credential_ids=["cred_fb1"],
    )

    with patch("skyvern.forge.sdk.workflow.context_manager.app") as mock_app:
        mock_app.DATABASE.workflow_run_credential_selections.get_selection = AsyncMock(return_value=pinned_selection)
        resolved = await context.resolve_credential_parameter_id(parameter, "org_test")

    assert resolved == expected


def test_yaml_to_credential_parameter_round_trip_preserves_fallback_fields() -> None:
    yaml_definition = WorkflowDefinitionYAML(
        parameters=[
            CredentialParameterYAML(
                key="login_cred",
                credential_id="cred_primary",
                fallback_credential_ids=["cred_fb1", "cred_fb2"],
                fallback_trigger=ANY_FAILURE,
            )
        ],
        blocks=[],
    )

    definition = convert_workflow_definition(yaml_definition, workflow_id="wf_test")
    parameter = definition.parameters[0]

    assert isinstance(parameter, CredentialParameter)
    assert parameter.fallback_credential_ids == ["cred_fb1", "cred_fb2"]
    assert parameter.fallback_trigger == ANY_FAILURE


@pytest.mark.asyncio
async def test_mark_workflow_run_as_failed_invokes_fallback_hook() -> None:
    service = WorkflowService()
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])
    service._update_workflow_run_status = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service._schedule_credential_fallback_retry = MagicMock()  # type: ignore[method-assign]

    result = await service.mark_workflow_run_as_failed(
        workflow_run_id="wr_failed",
        failure_reason="login failed",
        failure_category=[{"category": "AUTH_FAILURE"}],
    )

    assert result == workflow_run
    service._schedule_credential_fallback_retry.assert_called_once_with(workflow_run)


@pytest.mark.asyncio
async def test_mark_workflow_run_as_terminated_invokes_fallback_hook() -> None:
    service = WorkflowService()
    workflow_run = _workflow_run(
        status=WorkflowRunStatus.terminated,
        failure_category=[{"category": "AUTH_FAILURE"}],
    )
    service._update_workflow_run_status = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service._schedule_credential_fallback_retry = MagicMock()  # type: ignore[method-assign]

    result = await service.mark_workflow_run_as_terminated(
        workflow_run_id="wr_failed",
        failure_reason="login failed",
        failure_category=[{"category": "AUTH_FAILURE"}],
    )

    assert result == workflow_run
    service._schedule_credential_fallback_retry.assert_called_once_with(workflow_run)


@pytest.mark.asyncio
async def test_fallback_hook_exception_does_not_raise() -> None:
    workflow_run = _workflow_run(failure_category=[{"category": "AUTH_FAILURE"}])

    with patch(
        "skyvern.forge.sdk.workflow.service.maybe_start_credential_fallback_retry",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await WorkflowService._start_credential_fallback_retry_best_effort(workflow_run)
