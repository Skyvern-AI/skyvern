"""LoginBlock preflight for an at-will credential that resolved to null (SKY-14006).

An at-will credential parameter (credential_id type, no default) is allowed to be absent, so a
run whose credential arrived under the wrong key starts anyway and only fails once the agent is
staring at a login form it cannot fill. The preflight names the parameter instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import LoginBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    CredentialParameter,
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)

_NOW = datetime.now(tz=timezone.utc)


def _workflow_run(browser_profile_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(browser_profile_id=browser_profile_id)


def _login_block(parameters: list) -> LoginBlock:
    return LoginBlock(
        label="login",
        parameters=parameters,
        output_parameter=OutputParameter(
            key="login_output",
            workflow_id="wf_test",
            output_parameter_id="op_login",
            created_at=_NOW,
            modified_at=_NOW,
        ),
    )


def _at_will_credential(key: str) -> WorkflowParameter:
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_id="wf_test",
        key=key,
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
        default_value=None,
        created_at=_NOW,
        modified_at=_NOW,
    )


def _run_context() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="Workflow",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )


def test_preflight_names_the_unresolved_credential_parameter() -> None:
    parameter = _at_will_credential("credentials_default")
    context = _run_context()
    context.parameters[parameter.key] = parameter
    context.values[parameter.key] = None

    reason = _login_block([parameter]).preflight_failure_reason(context, _workflow_run())

    assert reason is not None
    assert "credentials_default" in reason


def test_preflight_passes_when_the_credential_resolved() -> None:
    parameter = _at_will_credential("credentials_default")
    context = _run_context()
    context.parameters[parameter.key] = parameter
    context.values[parameter.key] = "cred_123"
    context.resolved_credential_parameter_ids[parameter.key] = "cred_123"

    assert _login_block([parameter]).preflight_failure_reason(context, _workflow_run()) is None


def test_preflight_passes_when_another_parameter_can_sign_in() -> None:
    """A workflow that also declares a real credential parameter still has a way to log in."""
    at_will = _at_will_credential("credentials_default")
    configured = CredentialParameter(
        credential_parameter_id="cp_test",
        workflow_id="wf_test",
        key="portal_credential",
        credential_id="cred_primary",
        created_at=_NOW,
        modified_at=_NOW,
    )
    context = _run_context()
    context.parameters[at_will.key] = at_will
    context.values[at_will.key] = None

    assert _login_block([at_will, configured]).preflight_failure_reason(context, _workflow_run()) is None


def test_preflight_passes_when_reusing_a_saved_browser_profile() -> None:
    """A run pinned to a saved browser profile already carries an authenticated session, so an
    unresolved at-will credential is the intended state, not a failure (SKY-14006 review)."""
    parameter = _at_will_credential("credentials_default")
    context = _run_context()
    context.parameters[parameter.key] = parameter
    context.values[parameter.key] = None

    reason = _login_block([parameter]).preflight_failure_reason(context, _workflow_run("bp_saved"))

    assert reason is None


def test_preflight_still_fails_when_skip_saved_profile_opts_out_of_reuse() -> None:
    """skip_saved_profile means this block logs in fresh even if the run has a saved profile
    pinned (e.g. a credential re-save) -- the saved-profile escape must not apply here."""
    parameter = _at_will_credential("credentials_default")
    context = _run_context()
    context.parameters[parameter.key] = parameter
    context.values[parameter.key] = None

    login_block = LoginBlock(
        label="login",
        parameters=[parameter],
        skip_saved_profile=True,
        output_parameter=OutputParameter(
            key="login_output",
            workflow_id="wf_test",
            output_parameter_id="op_login",
            created_at=_NOW,
            modified_at=_NOW,
        ),
    )

    reason = login_block.preflight_failure_reason(context, _workflow_run("bp_saved"))

    assert reason is not None
    assert "credentials_default" in reason
