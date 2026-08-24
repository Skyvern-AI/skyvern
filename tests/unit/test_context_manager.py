"""Tests for WorkflowRunContext initialization in context_manager."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition, WorkflowRunParameter
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext


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
