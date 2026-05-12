"""Tests for WorkflowService.setup_workflow_run batch parameter persistence.

Verifies that setup_workflow_run collects all parameter values first and
persists them in a single batch insert, and that validation failures
(missing params, invalid credentials, DB errors) are handled correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import InvalidCredentialId, MissingValueForParameter, WorkflowRunParameterPersistenceError
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.forge.sdk.workflow.service import WorkflowService


def _make_workflow_parameter(
    key: str,
    *,
    workflow_parameter_type: WorkflowParameterType = WorkflowParameterType.STRING,
    default_value: str | int | float | bool | dict | list | None = None,
) -> WorkflowParameter:
    now = datetime.now(tz=timezone.utc)
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_id="wf_test",
        key=key,
        workflow_parameter_type=workflow_parameter_type,
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


def _make_service_with_mocks(
    *,
    workflow_parameters: list[WorkflowParameter],
    batch_side_effect: Exception | None = None,
    single_side_effect: Exception | None = None,
) -> tuple[WorkflowService, SimpleNamespace, SimpleNamespace]:
    """Helper to build a WorkflowService with mocked internals for setup_workflow_run tests."""
    service = WorkflowService()
    workflow = SimpleNamespace(
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        proxy_location=None,
        webhook_callback_url=None,
        extra_http_headers=None,
        browser_profile_id=None,
        run_with="agent",
        code_version=None,
        adaptive_caching=False,
        sequential_key=None,
    )
    workflow_run = SimpleNamespace(workflow_run_id="wr_test", workflow_permanent_id="wpid_test")

    service.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)  # type: ignore[method-assign]
    service.create_workflow_run = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service.get_workflow_parameters = AsyncMock(return_value=workflow_parameters)  # type: ignore[method-assign]
    if batch_side_effect:
        service.create_workflow_run_parameters = AsyncMock(side_effect=batch_side_effect)  # type: ignore[method-assign]
    else:
        service.create_workflow_run_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    if single_side_effect:
        service.create_workflow_run_parameter = AsyncMock(side_effect=single_side_effect)  # type: ignore[method-assign]
    else:
        service.create_workflow_run_parameter = AsyncMock()  # type: ignore[method-assign]
    service.mark_workflow_run_as_failed = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]

    organization = SimpleNamespace(organization_id="org_test", organization_name="Test Org")
    return service, organization, workflow_run


@pytest.fixture(autouse=True)
def reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


@pytest.mark.asyncio
async def test_setup_workflow_run_raises_on_missing_required_parameters() -> None:
    """When required parameters have no value and no default, setup should raise MissingValueForParameter."""
    required_param = _make_workflow_parameter("api_key")  # no default_value
    service, organization, _ = _make_service_with_mocks(workflow_parameters=[required_param])

    request = WorkflowRequestBody(data={})  # no data for api_key

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)

        with pytest.raises(MissingValueForParameter):
            await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=request,
                workflow_permanent_id="wpid_test",
                organization=organization,
            )

    service.create_workflow_run_parameters.assert_not_awaited()
    service.mark_workflow_run_as_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_workflow_run_persistence_error_identifies_specific_failing_parameter() -> None:
    """When batch fails with multiple params, fallback to one-by-one should pinpoint the failing key."""
    params = [
        _make_workflow_parameter(
            "alpha_count", workflow_parameter_type=WorkflowParameterType.INTEGER, default_value="1"
        ),
        _make_workflow_parameter("middle_label", default_value="mid"),
        _make_workflow_parameter("zebra_url", default_value="https://zebra.example.com"),
    ]
    batch_error = IntegrityError("INSERT", {}, Exception("constraint failed"))
    single_error = IntegrityError("INSERT", {}, Exception("NOT NULL constraint on middle_label"))

    # Single insert succeeds for alpha_count, fails on middle_label
    async def _single_insert_side_effect(
        *, workflow_run_id: str, workflow_parameter: WorkflowParameter, value: object
    ) -> None:
        if workflow_parameter.key == "middle_label":
            raise single_error

    service, organization, _ = _make_service_with_mocks(
        workflow_parameters=params,
        batch_side_effect=batch_error,
    )
    service.create_workflow_run_parameter = AsyncMock(side_effect=_single_insert_side_effect)  # type: ignore[method-assign]

    request = WorkflowRequestBody(data={"alpha_count": 5, "middle_label": "test", "zebra_url": "https://z.com"})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)

        with pytest.raises(WorkflowRunParameterPersistenceError) as exc_info:
            await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=request,
                workflow_permanent_id="wpid_test",
                organization=organization,
            )

    error_message = str(exc_info.value)
    # Should identify only the failing parameter, not all three
    assert "middle_label" in error_message
    assert "alpha_count" not in error_message
    assert "zebra_url" not in error_message
    assert exc_info.value.__cause__ is single_error


@pytest.mark.asyncio
async def test_setup_workflow_run_raises_on_non_string_credential_id() -> None:
    """Credential ID parameters must be strings. Passing an int should raise InvalidCredentialId."""
    cred_param = _make_workflow_parameter(
        "credential",
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
    )
    service, organization, _ = _make_service_with_mocks(workflow_parameters=[cred_param])

    request = WorkflowRequestBody(data={"credential": 12345})  # not a string

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)

        with pytest.raises(InvalidCredentialId):
            await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=request,
                workflow_permanent_id="wpid_test",
                organization=organization,
            )

    service.create_workflow_run_parameters.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_workflow_run_batches_credential_validation() -> None:
    """N credential parameters should issue a single get_credentials_by_ids call, not N get_credential calls."""
    cred_params = [
        _make_workflow_parameter(
            f"cred_param_{i}",
            workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
            default_value=f"cred_id_{i}",
        )
        for i in range(3)
    ]
    service, organization, _ = _make_service_with_mocks(workflow_parameters=cred_params)

    request = WorkflowRequestBody(data={})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(
            return_value=[SimpleNamespace(credential_id=f"cred_id_{i}") for i in range(3)]
        )
        mock_app.DATABASE.credentials.get_credential = AsyncMock()

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    mock_app.DATABASE.credentials.get_credentials_by_ids.assert_awaited_once()
    args, kwargs = mock_app.DATABASE.credentials.get_credentials_by_ids.call_args
    passed_ids = args[0] if args else kwargs["credential_ids"]
    assert sorted(passed_ids) == ["cred_id_0", "cred_id_1", "cred_id_2"]
    mock_app.DATABASE.credentials.get_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_workflow_run_skips_credential_lookup_when_no_credentials() -> None:
    """Workflows without credential params should not call get_credentials_by_ids at all."""
    string_param = _make_workflow_parameter("name", default_value="value")
    service, organization, _ = _make_service_with_mocks(workflow_parameters=[string_param])

    request = WorkflowRequestBody(data={})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(return_value=[])

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    mock_app.DATABASE.credentials.get_credentials_by_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_workflow_run_raises_invalid_credential_when_missing() -> None:
    """A single missing credential should raise InvalidCredentialId."""
    cred_param = _make_workflow_parameter(
        "credential",
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
        default_value="cred_missing",
    )
    service, organization, _ = _make_service_with_mocks(workflow_parameters=[cred_param])

    request = WorkflowRequestBody(data={})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(return_value=[])

        with pytest.raises(InvalidCredentialId) as exc_info:
            await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=request,
                workflow_permanent_id="wpid_test",
                organization=organization,
            )

    assert "cred_missing" in str(exc_info.value)


@pytest.mark.asyncio
async def test_setup_workflow_run_surfaces_all_missing_credentials() -> None:
    """When multiple credentials are missing, the error should mention every missing id."""
    cred_params = [
        _make_workflow_parameter(
            f"cred_param_{i}",
            workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
            default_value=f"cred_id_{i}",
        )
        for i in range(3)
    ]
    service, organization, _ = _make_service_with_mocks(workflow_parameters=cred_params)

    request = WorkflowRequestBody(data={})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        # Only cred_id_0 exists; cred_id_1 and cred_id_2 are missing.
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(
            return_value=[SimpleNamespace(credential_id="cred_id_0")]
        )

        with pytest.raises(InvalidCredentialId) as exc_info:
            await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=request,
                workflow_permanent_id="wpid_test",
                organization=organization,
            )

    error_msg = str(exc_info.value)
    assert "cred_id_1" in error_msg
    assert "cred_id_2" in error_msg
    assert "cred_id_0" not in error_msg


@pytest.mark.asyncio
async def test_setup_workflow_run_dedupes_repeated_credential_ids() -> None:
    """Repeated credential ids across params should be deduped before the IN-query."""
    cred_params = [
        _make_workflow_parameter(
            f"cred_param_{i}",
            workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
            default_value="cred_shared",
        )
        for i in range(3)
    ]
    service, organization, _ = _make_service_with_mocks(workflow_parameters=cred_params)

    request = WorkflowRequestBody(data={})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(
            return_value=[SimpleNamespace(credential_id="cred_shared")]
        )

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    args, kwargs = mock_app.DATABASE.credentials.get_credentials_by_ids.call_args
    passed_ids = args[0] if args else kwargs["credential_ids"]
    assert passed_ids == ["cred_shared"]


@pytest.mark.asyncio
async def test_setup_workflow_run_preserves_parent_loop_state_when_replacing_context() -> None:
    service, organization, _ = _make_service_with_mocks(workflow_parameters=[])

    loop_state = {"downloaded_file_signatures_before_iteration": [("a.pdf", "abc", "https://files/a.pdf")]}
    parent_context = SkyvernContext(
        organization_id="org_test",
        organization_name="Test Org",
        workflow_run_id="wr_parent",
        root_workflow_run_id="wr_root",
        run_id="wr_parent",
        loop_internal_state=loop_state,
    )
    skyvern_context.set(parent_context)

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=WorkflowRequestBody(data={}),
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    current_context = skyvern_context.current()
    assert current_context is not None
    assert current_context.workflow_run_id == "wr_test"
    assert current_context.run_id == "wr_parent"
    assert current_context.root_workflow_run_id == "wr_root"
    assert current_context.loop_internal_state == loop_state
    assert current_context.loop_internal_state is not loop_state


@pytest.mark.asyncio
async def test_setup_workflow_run_opens_one_outer_session() -> None:
    """setup_workflow_run wraps its body in exactly one outer ``Session()`` context."""

    params = [_make_workflow_parameter("k", default_value="v")]
    service, organization, _ = _make_service_with_mocks(workflow_parameters=params)

    session_open_count = 0

    class _Counter:
        async def __aenter__(self) -> _Counter:
            nonlocal session_open_count
            session_open_count += 1
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def rollback(self) -> None:
            return None

    request = WorkflowRequestBody(data={"k": "v"})
    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        mock_app.DATABASE.workflow_runs.Session = lambda: _Counter()
        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    assert session_open_count == 1, (
        f"Expected exactly one outer Session() open in setup_workflow_run, got {session_open_count}"
    )


@pytest.mark.asyncio
async def test_setup_workflow_run_rolls_back_outer_session_on_batch_failure() -> None:
    """When the batched parameter insert raises, the outer session must be rolled back
    before the per-parameter fallback reuses it - otherwise the fallback runs on a
    session whose transaction is in error state."""

    params = [
        _make_workflow_parameter("a", default_value="1"),
        _make_workflow_parameter("b", default_value="2"),
    ]
    batch_error = IntegrityError("INSERT", {}, Exception("constraint failed"))

    fallback_call_index: list[int] = []
    rollback_index: list[int] = []
    call_counter = {"n": 0}

    async def _fallback_insert(*, workflow_run_id: str, workflow_parameter: WorkflowParameter, value: object) -> None:
        call_counter["n"] += 1
        fallback_call_index.append(call_counter["n"])

    service, organization, _ = _make_service_with_mocks(
        workflow_parameters=params,
        batch_side_effect=batch_error,
    )
    service.create_workflow_run_parameter = AsyncMock(side_effect=_fallback_insert)  # type: ignore[method-assign]

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def rollback(self) -> None:
            call_counter["n"] += 1
            rollback_index.append(call_counter["n"])

    request = WorkflowRequestBody(data={"a": "1", "b": "2"})
    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        mock_app.DATABASE.workflow_runs.Session = lambda: _Session()
        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    assert rollback_index, "Expected rollback() to be called on the outer session after batch failure"
    assert fallback_call_index, "Expected the fallback path to run after rollback"
    assert rollback_index[0] < fallback_call_index[0], (
        f"rollback must precede fallback insert; got rollback at {rollback_index} fallback at {fallback_call_index}"
    )
