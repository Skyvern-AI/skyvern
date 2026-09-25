"""Tests for WorkflowRunContext initialization in context_manager."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from google.api_core.exceptions import PermissionDenied, ServiceUnavailable
from structlog.testing import capture_logs

from skyvern.exceptions import (
    BitwardenAccessDeniedError,
    BitwardenListItemsError,
    CredentialItemNotFoundError,
    CredentialSourceNotConfiguredError,
    HttpException,
    OnePasswordServiceUnavailableError,
    OnePasswordSessionExpiredError,
)
from skyvern.forge.sdk.api.azure import AsyncAzureVaultClient
from skyvern.forge.sdk.schemas.credentials import (
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    PasswordCredential,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import bitwarden as bitwarden_module
from skyvern.forge.sdk.services.credential.azure_credential_vault_service import AzureCredentialVaultService
from skyvern.forge.sdk.services.credential.custom_credential_vault_service import (
    CustomCredentialConfigurationError,
    CustomCredentialNotConfiguredError,
)
from skyvern.forge.sdk.workflow import context_manager as cm
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.credential_fetch_outcome import (
    RUN_CREDENTIAL_FETCH_FINISHED_MESSAGE,
    classify_credential_fetch_failure,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    AzureVaultCredentialParameter,
    BitwardenLoginCredentialParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition, WorkflowRunParameter
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext
from tests.unit.scoped_asyncio import ScopedAsyncio


def _make_workflow_parameter(
    key: str,
    *,
    workflow_parameter_type: WorkflowParameterType = WorkflowParameterType.STRING,
    default_value: str | None = None,
) -> WorkflowParameter:
    now = datetime.now(UTC)
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=workflow_parameter_type,
        key=key,
        workflow_id="wf_test",
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


def _make_run_parameter(
    parameter: WorkflowParameter, value: bool | int | float | str | dict | list
) -> WorkflowRunParameter:
    return WorkflowRunParameter(
        workflow_run_id="wr_test",
        workflow_parameter_id=parameter.workflow_parameter_id,
        value=value,
        created_at=datetime.now(UTC),
    )


def _make_workflow(parameters: list[WorkflowParameter]) -> Workflow:
    now = datetime.now(UTC)
    return Workflow(
        workflow_id="wf_test",
        organization_id="org_test",
        title="Test",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=parameters, blocks=[]),
        created_at=now,
        modified_at=now,
    )


def _make_organization() -> Organization:
    now = datetime.now(UTC)
    return Organization(
        organization_id="org_test",
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )


class TestAtWillCredentialBackfill:
    """An absent at-will credential (credential_id type, no default) must resolve to an
    explicit None in the run context so blocks and templates referencing it do not KeyError.
    The backfill is scoped to that case: it never invents values for other parameters."""

    @pytest.mark.asyncio
    async def test_absent_at_will_credential_resolves_to_none(self) -> None:
        at_will_cred = _make_workflow_parameter("opt_cred", workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID)
        provided = _make_workflow_parameter("provided_key")
        workflow = _make_workflow([at_will_cred, provided])

        context = await WorkflowRunContext.init(
            aws_client=MagicMock(),
            organization=_make_organization(),
            workflow_run_id="wr_test",
            workflow_title="Test",
            workflow_id="wf_test",
            workflow_permanent_id="wpid_test",
            workflow_parameter_tuples=[(provided, _make_run_parameter(provided, "hello"))],
            workflow_output_parameters=[],
            context_parameters=[],
            secret_parameters=[],
            workflow=workflow,
        )

        assert context.values["opt_cred"] is None
        assert context.get_parameter("opt_cred") is at_will_cred
        assert context.values["provided_key"] == "hello"

    @pytest.mark.asyncio
    async def test_absent_non_credential_is_not_backfilled(self) -> None:
        required = _make_workflow_parameter("required_key")
        workflow = _make_workflow([required])

        context = await WorkflowRunContext.init(
            aws_client=MagicMock(),
            organization=_make_organization(),
            workflow_run_id="wr_test",
            workflow_title="Test",
            workflow_id="wf_test",
            workflow_permanent_id="wpid_test",
            workflow_parameter_tuples=[],
            workflow_output_parameters=[],
            context_parameters=[],
            secret_parameters=[],
            workflow=workflow,
        )

        assert not context.has_value("required_key")

    @pytest.mark.asyncio
    async def test_credential_with_default_is_not_backfilled_to_none(self) -> None:
        # A credential with a default is resolved to a real credential upstream (a run
        # parameter row); the at-will backfill must not shadow it with None.
        with_default_cred = _make_workflow_parameter(
            "portal_cred",
            workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
            default_value="cred_abc",
        )
        workflow = _make_workflow([with_default_cred])

        context = await WorkflowRunContext.init(
            aws_client=MagicMock(),
            organization=_make_organization(),
            workflow_run_id="wr_test",
            workflow_title="Test",
            workflow_id="wf_test",
            workflow_permanent_id="wpid_test",
            workflow_parameter_tuples=[],
            workflow_output_parameters=[],
            context_parameters=[],
            secret_parameters=[],
            workflow=workflow,
        )

        assert not context.has_value("portal_cred")


class TestCredentialTemplateEntriesShape:
    """`credential_template_entries` decides which credential secrets reach block templates, and a
    password-less credential registers no password placeholder — so the password shape is now keyed
    on `username` alone. Pin that the widened check still scopes secrets to password credentials."""

    def test_password_less_credential_exposes_username_and_empty_password(self) -> None:
        context = FakeWorkflowRunContext(
            values={
                "portal_cred": {
                    "context": "credential",
                    "username": "secret_username_id",
                },
            },
            secrets={"secret_username_id": "user@example.com"},
        )

        entries = context.credential_template_entries(["portal_cred"], resolve_credential_dicts=True)

        assert entries["portal_cred_real_username"] == "user@example.com"
        assert entries["portal_cred_real_password"] == ""
        assert entries["portal_cred"] == {"username": "user@example.com"}

    def test_password_credential_still_exposes_both_secrets(self) -> None:
        context = FakeWorkflowRunContext(
            values={
                "portal_cred": {
                    "context": "credential",
                    "username": "secret_username_id",
                    "password": "secret_password_id",
                },
            },
            secrets={"secret_username_id": "user@example.com", "secret_password_id": "hunter2"},
        )

        entries = context.credential_template_entries(["portal_cred"], resolve_credential_dicts=True)

        assert entries["portal_cred_real_username"] == "user@example.com"
        assert entries["portal_cred_real_password"] == "hunter2"

    def test_credit_card_credential_registers_no_password_entries(self) -> None:
        # Card credentials carry no `username`, so the widened check must not start emitting
        # spurious _real_username/_real_password entries for them.
        context = FakeWorkflowRunContext(
            values={
                "card_cred": {
                    "context": "credential",
                    "card_number": "secret_card_id",
                    "card_cvv": "secret_cvv_id",
                },
            },
            secrets={"secret_card_id": "4111111111111111", "secret_cvv_id": "123"},
        )

        entries = context.credential_template_entries(["card_cred"], resolve_credential_dicts=True)

        assert entries == {}

    def test_undeclared_credential_is_never_exposed(self) -> None:
        context = FakeWorkflowRunContext(
            values={
                "portal_cred": {
                    "context": "credential",
                    "username": "secret_username_id",
                },
            },
            secrets={"secret_username_id": "user@example.com"},
        )

        assert context.credential_template_entries([], resolve_credential_dicts=True) == {}


_FETCH_LINE_FIELDS = {
    "event",
    "log_level",
    "provider",
    "parameter_type",
    "outcome",
    "failure_type",
    "duration_seconds",
}


class _FakeAzureVault:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    async def __aenter__(self) -> "_FakeAzureVault":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_secret(self, secret_name: str, vault_name: str) -> str | None:
        return self._secrets.get(secret_name)


def _install_credential_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_credential: object | None = None,
    vault_service: object | None = None,
    azure_secrets: dict[str, str] | None = None,
) -> None:
    monkeypatch.setattr(
        cm,
        "app",
        SimpleNamespace(
            DATABASE=SimpleNamespace(
                organizations=SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=None)),
                credentials=SimpleNamespace(get_credential=AsyncMock(return_value=db_credential)),
            ),
            CREDENTIAL_VAULT_SERVICES={CredentialVaultType.AZURE_VAULT: vault_service},
            AGENT_FUNCTION=SimpleNamespace(
                process_registered_credential_item=AsyncMock(side_effect=lambda **kwargs: kwargs["credential_item"]),
                parse_enterprise_totp_secret=AsyncMock(return_value=None),
            ),
            AZURE_CLIENT_FACTORY=SimpleNamespace(create_default=lambda: _FakeAzureVault(azure_secrets or {})),
            EXPERIMENTATION_PROVIDER=SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=False)),
        ),
    )


async def _init_context(
    *,
    workflow_parameter_tuples: list[tuple[WorkflowParameter, WorkflowRunParameter]] | None = None,
    secret_parameters: list[Any] | None = None,
) -> WorkflowRunContext:
    return await WorkflowRunContext.init(
        aws_client=MagicMock(),
        organization=_make_organization(),
        workflow_run_id="wr_test",
        workflow_title="Test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_parameter_tuples=workflow_parameter_tuples or [],
        workflow_output_parameters=[],
        context_parameters=[],
        secret_parameters=secret_parameters or [],
    )


def _fetch_lines(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [log for log in logs if log["event"] == RUN_CREDENTIAL_FETCH_FINISHED_MESSAGE]


def _chained(error: BaseException, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


def _raised_while_handling(error: BaseException, handled: BaseException, *, suppress: bool = False) -> BaseException:
    try:
        try:
            raise handled
        except type(handled):
            if suppress:
                raise error from None
            raise error
    except type(error) as raised:
        return raised


class TestRunCredentialFetchOutcome:
    """Each Run-path credential read logs exactly one bounded outcome line, after its retries and
    fallbacks, so a read they recover never counts as a provider failure."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failed_attempts", "outcome", "failure_type"),
        [(1, "succeeded", None), (None, "provider_error", "TimeoutError")],
    )
    async def test_bitwarden_read_logs_one_outcome_after_its_retry_ladder(
        self,
        monkeypatch: pytest.MonkeyPatch,
        failed_attempts: int | None,
        outcome: str,
        failure_type: str | None,
    ) -> None:
        _install_credential_app(monkeypatch)
        monkeypatch.setattr(cm.settings, "BITWARDEN_CLIENT_ID", "client-id")
        monkeypatch.setattr(cm.settings, "BITWARDEN_CLIENT_SECRET", "client-secret")
        monkeypatch.setattr(cm.settings, "BITWARDEN_MASTER_PASSWORD", "master-password")
        monkeypatch.setattr(bitwarden_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
        attempts = 0

        async def vault_read(**kwargs: object) -> dict[str, str]:
            nonlocal attempts
            attempts += 1
            if failed_attempts is None or attempts <= failed_attempts:
                raise TimeoutError()
            return {
                bitwarden_module.BitwardenConstants.USERNAME: "user@example.com",
                bitwarden_module.BitwardenConstants.PASSWORD: "synthetic-password",
                bitwarden_module.BitwardenConstants.TOTP: "",
            }

        monkeypatch.setattr(bitwarden_module.BitwardenService, "_get_secret_value_from_url", vault_read)
        url_parameter = _make_workflow_parameter("target_url")
        now = datetime.now(UTC)
        login = BitwardenLoginCredentialParameter(
            key="portal_login",
            bitwarden_login_credential_parameter_id="blc_1",
            workflow_id="wf_test",
            bitwarden_client_id_aws_secret_key="unused",
            bitwarden_client_secret_aws_secret_key="unused",
            bitwarden_master_password_aws_secret_key="unused",
            url_parameter_key="target_url",
            created_at=now,
            modified_at=now,
        )
        url_input = (url_parameter, _make_run_parameter(url_parameter, "https://example.com"))

        with capture_logs() as logs:
            if failed_attempts is None:
                with pytest.raises(BitwardenListItemsError):
                    await _init_context(workflow_parameter_tuples=[url_input], secret_parameters=[login])
            else:
                await _init_context(workflow_parameter_tuples=[url_input], secret_parameters=[login])

        lines = _fetch_lines(logs)
        assert [(line["provider"], line["outcome"], line["failure_type"]) for line in lines] == [
            ("bitwarden", outcome, failure_type)
        ]
        assert set(lines[0]) == _FETCH_LINE_FIELDS
        assert "synthetic-password" not in json.dumps(lines)

    @pytest.mark.asyncio
    async def test_missing_vault_key_is_a_missing_binding_not_a_provider_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_credential_app(monkeypatch, azure_secrets={"portal-user": "user@example.com"})
        now = datetime.now(UTC)
        parameter = AzureVaultCredentialParameter(
            key="portal_login",
            azure_vault_credential_parameter_id="avcp_1",
            workflow_id="wf_test",
            vault_name="customer-vault",
            username_key="portal-user",
            password_key="portal-password",
            created_at=now,
            modified_at=now,
        )

        with capture_logs() as logs, pytest.raises(ValueError, match="password not found"):
            await _init_context(secret_parameters=[parameter])

        assert [(line["provider"], line["parameter_type"], line["outcome"]) for line in _fetch_lines(logs)] == [
            ("azure_vault", "azure_vault_credential", "missing_binding")
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored", "vault_item_exists", "provider", "outcome"),
        [
            (True, True, "azure_vault", "succeeded"),
            (False, True, "unknown", "missing_binding"),
            (True, False, "azure_vault", "missing_binding"),
        ],
    )
    async def test_credential_id_read_logs_the_vault_it_resolved_to(
        self, monkeypatch: pytest.MonkeyPatch, stored: bool, vault_item_exists: bool, provider: str, outcome: str
    ) -> None:
        db_credential = SimpleNamespace(
            credential_id="cred_1",
            organization_id="org_test",
            item_id="item_1",
            vault_type=CredentialVaultType.AZURE_VAULT,
            totp_identifier=None,
            run_sequentially=False,
            tested_url=None,
        )
        item = CredentialItem(
            item_id="item_1",
            name="Portal",
            credential_type=CredentialType.PASSWORD,
            credential=PasswordCredential(username="user@example.com", password="synthetic-password"),
        )
        vault_service: object = SimpleNamespace(get_credential_item=AsyncMock(return_value=item))
        if not vault_item_exists:
            deleted_secret = SimpleNamespace(get_secret=AsyncMock(return_value=None))
            vault_service = AzureCredentialVaultService(cast(AsyncAzureVaultClient, deleted_secret), "vault")
        _install_credential_app(
            monkeypatch,
            db_credential=db_credential if stored else None,
            vault_service=vault_service,
        )
        credential = _make_workflow_parameter(
            "portal_cred", workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID
        )
        credential_input = (credential, _make_run_parameter(credential, "cred_1"))

        with capture_logs() as logs:
            if not stored:
                with pytest.raises(Exception, match="Could not find credential parameter"):
                    await _init_context(workflow_parameter_tuples=[credential_input])
            elif not vault_item_exists:
                with pytest.raises(ValueError, match="Azure Credential Vault secret not found"):
                    await _init_context(workflow_parameter_tuples=[credential_input])
            else:
                await _init_context(workflow_parameter_tuples=[credential_input])

        assert [(line["provider"], line["parameter_type"], line["outcome"]) for line in _fetch_lines(logs)] == [
            (provider, "credential_id", outcome)
        ]

    @pytest.mark.parametrize(
        ("error", "customer_owned", "outcome"),
        [
            (_chained(BitwardenListItemsError("all retries failed"), TimeoutError()), False, "provider_error"),
            (
                _raised_while_handling(BitwardenListItemsError("all retries failed"), TimeoutError()),
                False,
                "provider_error",
            ),
            (
                _raised_while_handling(BitwardenListItemsError("all retries failed"), TimeoutError(), suppress=True),
                False,
                "unexpected",
            ),
            (_chained(Exception("fetch failed"), HttpException(404, "http://vault/item")), False, "missing_binding"),
            (_chained(Exception("fetch failed"), HttpException(503, "http://vault/item")), False, "provider_error"),
            (HttpException(401, "http://vault/item"), True, "customer_config"),
            (HttpException(401, "http://vault/item"), False, "provider_error"),
            (HttpResponseError(message="no response"), False, "provider_error"),
            (ClientAuthenticationError(message="client secret expired"), True, "customer_config"),
            (PermissionDenied("denied"), False, "provider_error"),
            (ServiceUnavailable("down"), False, "provider_error"),
            (OnePasswordServiceUnavailableError(status_code=503), True, "provider_error"),
            (OnePasswordSessionExpiredError("expired"), True, "customer_config"),
            (BitwardenAccessDeniedError(), True, "customer_config"),
            (CredentialItemNotFoundError("no such key"), True, "missing_binding"),
            (CredentialSourceNotConfiguredError("Vault ID is missing"), True, "customer_config"),
            (CustomCredentialNotConfiguredError("org_test"), True, "customer_config"),
            (CustomCredentialConfigurationError("invalid configuration"), True, "customer_config"),
            (ValueError("unparseable item"), False, "unexpected"),
        ],
    )
    def test_failure_classes_separate_provider_faults_from_customer_causes(
        self, error: BaseException, customer_owned: bool, outcome: str
    ) -> None:
        assert classify_credential_fetch_failure(error, customer_owned=customer_owned)[0] == outcome
