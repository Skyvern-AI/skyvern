"""SEND_EMAIL block conversion (SKY-12062): a send_email block that references
an smtp_* secret parameter which was never declared in the workflow's parameters
must surface as a handled validation error (422), not a bare KeyError (500)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import SendEmailBlock
from skyvern.forge.sdk.workflow.models.parameter import AWSSecretParameter, OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import SendEmailBlockYAML


def _output_parameter(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=f"{label}_output",
        output_parameter_id="op_1",
        workflow_id="w_1",
        created_at=now,
        modified_at=now,
    )


def _aws_secret_parameter(key: str) -> AWSSecretParameter:
    now = datetime.now(UTC)
    return AWSSecretParameter(
        key=key,
        aws_secret_parameter_id=f"asp_{key}",
        workflow_id="w_1",
        aws_key=key,
        created_at=now,
        modified_at=now,
    )


def _send_email_block_yaml() -> SendEmailBlockYAML:
    return SendEmailBlockYAML(
        label="send_email",
        smtp_host_secret_parameter_key="smtp_host",
        smtp_port_secret_parameter_key="smtp_port",
        smtp_username_secret_parameter_key="smtp_username",
        smtp_password_secret_parameter_key="smtp_password",
        sender="sender@example.com",
        recipients=["recipient@example.com"],
        subject="subject",
        body="body",
    )


def test_undeclared_smtp_parameter_raises_invalid_workflow_definition() -> None:
    parameters = {"send_email_output": _output_parameter("send_email")}
    with pytest.raises(InvalidWorkflowDefinition) as exc_info:
        block_yaml_to_block(_send_email_block_yaml(), parameters)
    assert "smtp_host" in str(exc_info.value)


def test_declared_smtp_parameters_convert_successfully() -> None:
    parameters = {
        "send_email_output": _output_parameter("send_email"),
        "smtp_host": _aws_secret_parameter("smtp_host"),
        "smtp_port": _aws_secret_parameter("smtp_port"),
        "smtp_username": _aws_secret_parameter("smtp_username"),
        "smtp_password": _aws_secret_parameter("smtp_password"),
    }
    block = block_yaml_to_block(_send_email_block_yaml(), parameters)
    assert isinstance(block, SendEmailBlock)
    assert block.smtp_host.key == "smtp_host"
    assert block.smtp_password.key == "smtp_password"
