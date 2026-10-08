"""SEND_EMAIL block tests.

- SKY-12062: a send_email block that references an smtp_* secret parameter which was never
  declared in the workflow's parameters must surface as a handled validation error (422),
  not a bare KeyError (500).
- SKY-14062: optional custom SMTP settings (custom_smtp_*) route the block through the
  user's SMTP server; when absent the default platform sender path is unchanged, and the
  custom password is never echoed in error messages.
- SKY-15585: one Recipients entry may hold several comma- or semicolon-separated addresses
  (a workflow parameter substituted into the editor's comma-separated field); both mail
  blocks deliver to every address, and an invalid one fails the block without being echoed.
- SKY-15919: `body_format: html` on either block sends a multipart/alternative message (generated
  plain-text part + sanitized HTML part); the default `text` path is byte-identical to before.
- SKY-16062: the subject is exactly what the user wrote — no run id is appended. Users who
  want it place {{workflow_run_id}} themselves.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import pickle
import re
import smtplib
import ssl
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes
from email import policy as email_policy
from email.message import EmailMessage
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import libcst as cst
import pytest
import pytest_asyncio
from email_validator import EmailUndeliverableError, validate_email
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.config import settings
from skyvern.core.script_generations.generate_script import _build_send_email_statement
from skyvern.exceptions import BlockedHost, UnresolvableHost
from skyvern.forge import app
from skyvern.forge.sdk.api import email as email_api
from skyvern.forge.sdk.api.email import InvalidEmailRecipient, send, validate_recipients
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import (
    Base,
    GmailSendDispatchModel,
    GoogleOAuthCredentialModel,
    WorkflowModel,
    WorkflowRunBlockModel,
    WorkflowRunModel,
    WorkflowRunOutputParameterModel,
)
from skyvern.forge.sdk.services import google_oauth_service
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import (
    CustomSMTPAuthenticationFailed,
    CustomSMTPConnectionFailed,
    InvalidEmailClientConfiguration,
    InvalidWorkflowDefinition,
    NoValidEmailRecipient,
)
from skyvern.forge.sdk.workflow.models.block import (
    ForLoopBlock,
    HumanInteractionBlock,
    SendEmailBlock,
    _send_via_custom_smtp,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PLATFORM_SMTP_AWS_KEYS,
    UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY,
    AWSSecretParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition, WorkflowRunStatus
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block, convert_workflow_definition
from skyvern.schemas.emails import EmailBodyFormat
from skyvern.schemas.workflows import (
    AWSSecretParameterYAML,
    BlockResult,
    BlockStatus,
    BlockType,
    ForLoopBlockYAML,
    HumanInteractionBlockYAML,
    SendEmailBlockYAML,
    WhileLoopBlockYAML,
    WorkflowDefinitionYAML,
)
from skyvern.services import script_service
from skyvern.services.email import gmail as gmail_service


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


def _send_email_block_yaml(**overrides: object) -> SendEmailBlockYAML:
    fields: dict = {
        "label": "send_email",
        "smtp_host_secret_parameter_key": "smtp_host",
        "smtp_port_secret_parameter_key": "smtp_port",
        "smtp_username_secret_parameter_key": "smtp_username",
        "smtp_password_secret_parameter_key": "smtp_password",
        "sender": "sender@example.com",
        "recipients": ["recipient@example.com"],
        "subject": "subject",
        "body": "body",
    }
    fields.update(overrides)
    return SendEmailBlockYAML(**fields)


def _default_parameters() -> dict:
    return {
        "send_email_output": _output_parameter("send_email"),
        "smtp_host": _aws_secret_parameter("smtp_host"),
        "smtp_port": _aws_secret_parameter("smtp_port"),
        "smtp_username": _aws_secret_parameter("smtp_username"),
        "smtp_password": _aws_secret_parameter("smtp_password"),
    }


def _send_email_block(**overrides: object) -> SendEmailBlock:
    block = block_yaml_to_block(_send_email_block_yaml(**overrides), _default_parameters())
    assert isinstance(block, SendEmailBlock)
    return block


def _human_interaction_block(**overrides: object) -> HumanInteractionBlock:
    fields: dict = {
        "label": "approve",
        "output_parameter": _output_parameter("approve"),
        "recipients": ["approver@example.com"],
    }
    fields.update(overrides)
    return HumanInteractionBlock(**fields)


def _run_context(secret_values: dict[str, str] | None = None, values: dict[str, str] | None = None) -> MagicMock:
    secrets = secret_values or {}
    registered = values or {}
    context = MagicMock()
    context.organization_id = "o_1"
    context.get_original_secret_value_or_none.side_effect = lambda value: secrets.get(value)
    context.has_parameter.side_effect = lambda key: key in secrets
    # Mirror WorkflowRunContext: get_value raises for an unregistered key, get_value_or_none does not.
    context.get_value.side_effect = lambda key: registered[key]
    context.get_value_or_none.side_effect = registered.get
    context.has_value.side_effect = lambda key: key in registered
    return context


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


# --- SKY-14062: custom SMTP settings ---


def test_yaml_without_custom_smtp_leaves_default_path() -> None:
    block = _send_email_block()
    assert block.custom_smtp_host is None
    assert block.custom_smtp_port is None
    assert block.custom_smtp_username is None
    assert block.custom_smtp_password is None
    assert block.has_custom_smtp() is False


def test_yaml_custom_smtp_fields_round_trip_to_block() -> None:
    block = _send_email_block(
        custom_smtp_host="smtp.example.com",
        custom_smtp_port=2525,
        custom_smtp_username="user@example.com",
        custom_smtp_password="hunter2",
    )
    assert block.custom_smtp_host == "smtp.example.com"
    assert block.custom_smtp_port == 2525
    assert block.custom_smtp_username == "user@example.com"
    assert block.custom_smtp_password == "hunter2"
    assert block.has_custom_smtp() is True


def test_whitespace_custom_smtp_host_does_not_activate_custom_path() -> None:
    block = _send_email_block(custom_smtp_host="   ")
    assert block.has_custom_smtp() is False


def test_yaml_custom_smtp_only_converts_with_placeholder_default_parameters() -> None:
    yaml = _send_email_block_yaml(
        smtp_host_secret_parameter_key=None,
        smtp_port_secret_parameter_key=None,
        smtp_username_secret_parameter_key=None,
        smtp_password_secret_parameter_key=None,
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="hunter2",
    )
    block = block_yaml_to_block(yaml, {"send_email_output": _output_parameter("send_email")})
    assert isinstance(block, SendEmailBlock)
    assert block.has_custom_smtp() is True
    # The platform-sender parameters are structurally required but never read on the
    # custom path; the converter fills them with inert placeholders.
    assert block.smtp_host.aws_key == UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY
    assert block.smtp_password.aws_key == UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY
    # And the placeholders never surface in run-time parameter registration.
    context = MagicMock()
    context.has_parameter.return_value = False
    with patch.object(SendEmailBlock, "get_workflow_run_context", return_value=context):
        assert block.get_all_parameters("wr_1") == []


def test_yaml_omitted_smtp_keys_without_custom_smtp_provisions_platform_parameters() -> None:
    yaml = _send_email_block_yaml(
        smtp_host_secret_parameter_key=None,
        smtp_port_secret_parameter_key=None,
        smtp_username_secret_parameter_key=None,
        smtp_password_secret_parameter_key=None,
    )
    parameters = {"send_email_output": _output_parameter("send_email")}
    block = block_yaml_to_block(yaml, parameters, workflow_id="w_real_1")
    assert isinstance(block, SendEmailBlock)
    provisioned = {key: parameters[key] for key in PLATFORM_SMTP_AWS_KEYS}
    assert all(isinstance(parameter, AWSSecretParameter) for parameter in provisioned.values())
    assert {key: parameter.aws_key for key, parameter in provisioned.items()} == PLATFORM_SMTP_AWS_KEYS
    assert {parameter.workflow_id for parameter in provisioned.values()} == {"w_real_1"}
    assert all(parameter.aws_secret_parameter_id.startswith("asp_") for parameter in provisioned.values())
    assert block.smtp_host is parameters["smtp_host"]


def test_yaml_custom_smtp_with_stale_placeholder_keys_heals_editor_round_trip() -> None:
    # An API-created custom-SMTP-only workflow persists placeholder parameters with
    # ordinary keys ("smtp_host"). The editor serializes those keys back on save even
    # though the workflow declares no such parameters; with a custom host set they are
    # re-synthesized instead of failing the save.
    yaml = _send_email_block_yaml(custom_smtp_host="smtp.example.com")
    block = block_yaml_to_block(yaml, {"send_email_output": _output_parameter("send_email")})
    assert isinstance(block, SendEmailBlock)
    assert block.custom_smtp_host == "smtp.example.com"
    assert block.smtp_host.aws_key == UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY
    assert block.smtp_password.aws_key == UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY


def test_yaml_custom_smtp_with_non_secret_name_collision_uses_placeholder() -> None:
    # A regular (non-secret) parameter may share a canonical name like "smtp_host";
    # with a custom server it must not be passed into the AWS-secret-typed model fields.
    yaml = _send_email_block_yaml(custom_smtp_host="smtp.example.com")
    parameters = {
        "send_email_output": _output_parameter("send_email"),
        "smtp_host": _output_parameter("collide"),
    }
    collide = parameters["smtp_host"]
    block = block_yaml_to_block(yaml, parameters)
    assert isinstance(block, SendEmailBlock)
    assert block.smtp_host.aws_key == UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY
    assert set(parameters) == {"send_email_output", "smtp_host"}
    assert parameters["smtp_host"] is collide


@pytest.mark.asyncio
async def test_resolve_custom_smtp_defaults_port_and_passes_credentials() -> None:
    block = _send_email_block(
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="hunter2",
    )
    host, port, username, password = await block._resolve_custom_smtp_parameters(_run_context())
    assert host == "smtp.example.com"
    assert port == 587
    assert username == "user@example.com"
    assert password == "hunter2"


@pytest.mark.asyncio
async def test_resolve_custom_smtp_resolves_secret_references() -> None:
    block = _send_email_block(
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="obfuscated_ref",
    )
    context = _run_context({"obfuscated_ref": "real-password"})
    _, _, _, password = await block._resolve_custom_smtp_parameters(context)
    assert password == "real-password"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides, missing",
    [
        ({"custom_smtp_username": "user@example.com"}, "password"),
        ({"custom_smtp_password": "hunter2"}, "username"),
    ],
)
async def test_resolve_custom_smtp_requires_username_password_pair(overrides: dict, missing: str) -> None:
    block = _send_email_block(custom_smtp_host="smtp.example.com", **overrides)
    with pytest.raises(InvalidEmailClientConfiguration) as exc_info:
        await block._resolve_custom_smtp_parameters(_run_context())
    assert missing in str(exc_info.value)
    assert "hunter2" not in str(exc_info.value)


def test_format_templates_never_renders_literal_password_with_jinja_chars() -> None:
    block = _send_email_block(
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="pa{{7*7}}ss",
    )
    context = MagicMock()
    context.values = {}
    context.get_block_metadata.return_value = {}
    context.include_secrets_in_templates = False
    context.has_parameter.return_value = False
    block.format_potential_template_parameters(context)
    # Without the full-reference gate this would render to "pa49ss" (corrupted) and a
    # malformed literal would leak into the jinja failure message.
    assert block.custom_smtp_password == "pa{{7*7}}ss"


def test_format_templates_renders_full_reference_password() -> None:
    block = _send_email_block(
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="{{ smtp_password_param }}",
    )
    with patch.object(
        SendEmailBlock,
        "format_block_parameter_template_from_workflow_run_context",
        side_effect=lambda value, _context, **_kwargs: f"formatted:{value}",
    ):
        block.format_potential_template_parameters(MagicMock())
    assert block.custom_smtp_password == "formatted:{{ smtp_password_param }}"


@pytest.mark.parametrize("bad_port", [0, -1, 65536])
def test_custom_smtp_port_out_of_range_is_rejected(bad_port: int) -> None:
    with pytest.raises(ValueError):
        _send_email_block_yaml(custom_smtp_host="smtp.example.com", custom_smtp_port=bad_port)


def _message() -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "s"
    msg["To"] = "ops@example.com"
    msg.set_content("b")
    return msg


def _send_custom(
    host: str, port: int, username: str | None, password: str | None, connect_host: str | None = None
) -> None:
    _send_via_custom_smtp(
        host=host,
        port=port,
        connect_hosts=(connect_host or host,),
        username=username,
        password=password,
        message=_message(),
    )


def test_send_custom_smtp_falls_back_to_next_validated_address() -> None:
    client = MagicMock()
    with patch("smtplib.SMTP", side_effect=[OSError("[Errno 51] Network is unreachable"), client]) as smtp_cls:
        _send_via_custom_smtp(
            host="smtp.example.com",
            port=587,
            connect_hosts=("2001:db8::25", "203.0.113.7"),
            username=None,
            password=None,
            message=_message(),
        )
    assert [c.args[0] for c in smtp_cls.call_args_list] == ["2001:db8::25", "203.0.113.7"]
    client.send_message.assert_called_once()


def test_send_custom_smtp_falls_back_when_starttls_fails_on_first_address() -> None:
    broken = MagicMock()
    broken.starttls.side_effect = ssl.SSLError("TLS handshake failed")
    healthy = MagicMock()
    with patch("smtplib.SMTP", side_effect=[broken, healthy]) as smtp_cls:
        _send_via_custom_smtp(
            host="smtp.example.com",
            port=587,
            connect_hosts=("203.0.113.7", "203.0.113.8"),
            username=None,
            password=None,
            message=_message(),
        )
    assert [c.args[0] for c in smtp_cls.call_args_list] == ["203.0.113.7", "203.0.113.8"]
    broken.close.assert_called_once()
    broken.send_message.assert_not_called()
    healthy.starttls.assert_called_once()
    healthy.send_message.assert_called_once()


def test_send_custom_smtp_surfaces_last_error_when_all_addresses_fail() -> None:
    with patch(
        "smtplib.SMTP",
        side_effect=[OSError("[Errno 51] Network is unreachable"), OSError("[Errno 61] Connection refused")],
    ):
        with pytest.raises(CustomSMTPConnectionFailed) as exc_info:
            _send_via_custom_smtp(
                host="smtp.example.com",
                port=587,
                connect_hosts=("2001:db8::25", "203.0.113.7"),
                username=None,
                password=None,
                message=_message(),
            )
    assert "Connection refused" in str(exc_info.value)


def test_send_custom_smtp_starttls_path_verifies_tls_logs_in_and_sends() -> None:
    client = MagicMock()
    with (
        patch("smtplib.SMTP", return_value=client) as smtp_cls,
        patch("skyvern.forge.sdk.workflow.models.block._HostnamePinnedSMTPSSL") as smtp_ssl_cls,
    ):
        _send_custom("smtp.example.com", 587, "user@example.com", "hunter2")
    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
    smtp_ssl_cls.assert_not_called()
    client.starttls.assert_called_once()
    starttls_context = client.starttls.call_args.kwargs["context"]
    assert isinstance(starttls_context, ssl.SSLContext)
    assert starttls_context.verify_mode == ssl.CERT_REQUIRED
    assert starttls_context.check_hostname is True
    client.login.assert_called_once_with("user@example.com", "hunter2")
    client.send_message.assert_called_once()
    client.quit.assert_called_once_with()


def test_send_custom_smtp_port_465_uses_verified_implicit_tls() -> None:
    client = MagicMock()
    with (
        patch("smtplib.SMTP") as smtp_cls,
        patch("skyvern.forge.sdk.workflow.models.block._HostnamePinnedSMTPSSL", return_value=client) as smtp_ssl_cls,
    ):
        _send_custom("smtp.example.com", 465, "user@example.com", "hunter2")
    smtp_ssl_cls.assert_called_once()
    assert smtp_ssl_cls.call_args.args == ("smtp.example.com", 465)
    assert smtp_ssl_cls.call_args.kwargs["timeout"] == 30
    assert smtp_ssl_cls.call_args.kwargs["server_hostname"] == "smtp.example.com"
    ssl_context = smtp_ssl_cls.call_args.kwargs["context"]
    assert isinstance(ssl_context, ssl.SSLContext)
    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    smtp_cls.assert_not_called()
    client.starttls.assert_not_called()
    client.login.assert_called_once_with("user@example.com", "hunter2")
    client.send_message.assert_called_once()


def test_send_custom_smtp_dials_pinned_ip_but_verifies_hostname_tls() -> None:
    client = MagicMock()
    with patch("smtplib.SMTP", return_value=client) as smtp_cls:
        _send_custom("smtp.example.com", 587, None, None, connect_host="203.0.113.7")
    # The TCP target is the SSRF-validated IP; smtplib's starttls() reads `_host`
    # as the TLS server_hostname, which must stay the configured hostname.
    smtp_cls.assert_called_once_with("203.0.113.7", 587, timeout=30)
    assert client._host == "smtp.example.com"


def test_send_custom_smtp_port_465_pins_ip_and_verifies_hostname_tls() -> None:
    client = MagicMock()
    with patch("skyvern.forge.sdk.workflow.models.block._HostnamePinnedSMTPSSL", return_value=client) as smtp_ssl_cls:
        _send_custom("smtp.example.com", 465, None, None, connect_host="203.0.113.7")
    assert smtp_ssl_cls.call_args.args == ("203.0.113.7", 465)
    assert smtp_ssl_cls.call_args.kwargs["server_hostname"] == "smtp.example.com"


def test_send_custom_smtp_without_credentials_skips_login() -> None:
    client = MagicMock()
    with patch("smtplib.SMTP", return_value=client):
        _send_custom("smtp.example.com", 587, None, None)
    client.login.assert_not_called()
    client.send_message.assert_called_once()


def test_send_custom_smtp_connection_error_is_user_actionable_with_detail() -> None:
    with patch("smtplib.SMTP", side_effect=OSError("[Errno 8] nodename nor servname provided")):
        with pytest.raises(CustomSMTPConnectionFailed) as exc_info:
            _send_custom("bad.example.com", 587, "user@example.com", "hunter2")
    message = str(exc_info.value)
    assert re.search(r"bad\.example\.com:587", message)
    assert "Advanced settings" in message
    # Connect-stage errors carry no credentials; the OS detail makes the failure actionable.
    assert "nodename" in message
    assert "hunter2" not in message


def test_send_custom_smtp_auth_error_never_echoes_password() -> None:
    client = MagicMock()
    client.login.side_effect = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    with patch("smtplib.SMTP", return_value=client):
        with pytest.raises(CustomSMTPAuthenticationFailed) as exc_info:
            _send_custom("smtp.example.com", 587, "user@example.com", "hunter2")
    message = str(exc_info.value)
    assert "user@example.com" in message
    assert "hunter2" not in message
    assert exc_info.value.__cause__ is None  # server rejection text is never chained into logs
    client.send_message.assert_not_called()
    client.quit.assert_called_once_with()  # connection is torn down after the setup error


def test_send_custom_smtp_starttls_unsupported_suggests_port_465() -> None:
    client = MagicMock()
    client.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
    with patch("smtplib.SMTP", return_value=client):
        with pytest.raises(CustomSMTPConnectionFailed) as exc_info:
            _send_custom("smtp.example.com", 2525, "user@example.com", "hunter2")
    message = str(exc_info.value)
    assert "STARTTLS" in message
    assert "465" in message
    client.send_message.assert_not_called()
    # TLS-setup failures are torn down inside the per-address connect loop.
    client.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_connect_host_guard_blocks_internal_addresses() -> None:
    with patch(
        "skyvern.forge.sdk.workflow.models.block.resolve_fetch_host_ips",
        side_effect=BlockedHost(host="10.0.0.5"),
    ):
        with pytest.raises(CustomSMTPConnectionFailed) as exc_info:
            await SendEmailBlock._resolve_custom_smtp_connect_hosts("10.0.0.5", 587)
    assert "private or internal" in str(exc_info.value)


@pytest.mark.asyncio
async def test_connect_host_guard_reports_unresolvable_hostnames() -> None:
    with patch(
        "skyvern.forge.sdk.workflow.models.block.resolve_fetch_host_ips",
        side_effect=UnresolvableHost(host="nope.invalid"),
    ):
        with pytest.raises(CustomSMTPConnectionFailed) as exc_info:
            await SendEmailBlock._resolve_custom_smtp_connect_hosts("nope.invalid", 587)
    assert "could not be resolved" in str(exc_info.value)


@pytest.mark.asyncio
async def test_connect_host_guard_returns_all_validated_ips() -> None:
    with patch(
        "skyvern.forge.sdk.workflow.models.block.resolve_fetch_host_ips",
        return_value=("203.0.113.7", "203.0.113.8"),
    ):
        connect_hosts = await SendEmailBlock._resolve_custom_smtp_connect_hosts("smtp.example.com", 587)
    assert connect_hosts == ("203.0.113.7", "203.0.113.8")


@pytest.mark.asyncio
async def test_connect_host_guard_skips_resolution_when_internal_hosts_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.config import settings as skyvern_settings

    monkeypatch.setattr(skyvern_settings, "ALLOW_SMTP_INTERNAL_HOSTS", True)
    with patch("skyvern.forge.sdk.workflow.models.block.resolve_fetch_host_ips") as resolver:
        connect_hosts = await SendEmailBlock._resolve_custom_smtp_connect_hosts("smtp.internal", 587)
    resolver.assert_not_called()
    assert connect_hosts == ("smtp.internal",)


def test_generated_script_emits_custom_smtp_fields() -> None:
    block = {
        "label": "notify",
        "sender": "me@example.com",
        "recipients": ["ops@example.com"],
        "subject": "s",
        "body": "b",
        "file_attachments": [],
        "custom_smtp_host": "smtp.example.com",
        "custom_smtp_port": 465,
        "custom_smtp_username": "user@example.com",
        "custom_smtp_password": "skyvern_enc:aesgcm-v1:abc",
    }
    compact = cst.Module(body=[_build_send_email_statement(block)]).code.replace(" ", "").replace("\n", "")
    assert "custom_smtp_host='smtp.example.com'" in compact
    assert "custom_smtp_port=465" in compact
    assert "custom_smtp_username='user@example.com'" in compact
    assert "custom_smtp_password='skyvern_enc:aesgcm-v1:abc'" in compact


def test_generated_script_omits_absent_custom_smtp_fields() -> None:
    block = {
        "label": "notify",
        "sender": "me@example.com",
        "recipients": ["ops@example.com"],
        "subject": "s",
        "body": "b",
        "file_attachments": [],
        "custom_smtp_host": None,
        "custom_smtp_port": None,
        "custom_smtp_username": "",
        "custom_smtp_password": None,
    }
    compact = cst.Module(body=[_build_send_email_statement(block)]).code.replace(" ", "").replace("\n", "")
    assert "custom_smtp_" not in compact
    assert "label='notify'" in compact


def test_api_shaped_yaml_without_smtp_fields_converts_and_provisions() -> None:
    yaml = SendEmailBlockYAML(
        label="send_email",
        sender="sender@example.com",
        recipients=["recipient@example.com"],
        subject="subject",
        body="body",
    )
    assert yaml.smtp_host_secret_parameter_key is None
    parameters = {"send_email_output": _output_parameter("send_email")}
    block = block_yaml_to_block(yaml, parameters, workflow_id="w_real_1")
    assert isinstance(block, SendEmailBlock)
    assert sorted(key for key, value in parameters.items() if isinstance(value, AWSSecretParameter)) == sorted(
        PLATFORM_SMTP_AWS_KEYS
    )
    provisioned = {key: parameters[key] for key in PLATFORM_SMTP_AWS_KEYS}
    assert {key: parameter.aws_key for key, parameter in provisioned.items()} == PLATFORM_SMTP_AWS_KEYS
    assert {parameter.workflow_id for parameter in provisioned.values()} == {"w_real_1"}


def test_editor_declared_smtp_parameters_are_not_duplicated() -> None:
    yaml = SendEmailBlockYAML(
        label="send_email",
        sender="sender@example.com",
        recipients=["recipient@example.com"],
        subject="subject",
        body="body",
    )
    parameters = _default_parameters()
    keys_before = set(parameters)
    block = block_yaml_to_block(yaml, parameters, workflow_id="w_real_1")
    assert isinstance(block, SendEmailBlock)
    assert set(parameters) == keys_before
    assert block.smtp_host is parameters["smtp_host"]
    assert block.smtp_password is parameters["smtp_password"]


def test_explicit_undeclared_smtp_key_is_rejected_rather_than_provisioned() -> None:
    yaml = _send_email_block_yaml(smtp_host_secret_parameter_key="typo_smtp_host")
    parameters = {
        "send_email_output": _output_parameter("send_email"),
        "smtp_port": _aws_secret_parameter("smtp_port"),
        "smtp_username": _aws_secret_parameter("smtp_username"),
        "smtp_password": _aws_secret_parameter("smtp_password"),
    }
    with pytest.raises(InvalidWorkflowDefinition) as exc_info:
        block_yaml_to_block(yaml, parameters, workflow_id="w_real_1")
    assert "typo_smtp_host" in str(exc_info.value)
    assert "typo_smtp_host" not in parameters


def test_non_secret_smtp_host_parameter_is_rejected_without_custom_smtp() -> None:
    yaml = _send_email_block_yaml(
        smtp_host_secret_parameter_key=None,
        smtp_port_secret_parameter_key=None,
        smtp_username_secret_parameter_key=None,
        smtp_password_secret_parameter_key=None,
    )
    parameters = {
        "send_email_output": _output_parameter("send_email"),
        "smtp_host": _output_parameter("collide"),
    }
    with pytest.raises(InvalidWorkflowDefinition) as exc_info:
        block_yaml_to_block(yaml, parameters, workflow_id="w_real_1")
    message = str(exc_info.value)
    assert "smtp_host" in message
    assert "AWS secret" in message


def test_custom_smtp_block_provisions_no_platform_parameters() -> None:
    yaml = SendEmailBlockYAML(
        label="send_email",
        sender="sender@example.com",
        recipients=["recipient@example.com"],
        subject="subject",
        body="body",
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="hunter2",
    )
    parameters = {"send_email_output": _output_parameter("send_email")}
    block = block_yaml_to_block(yaml, parameters, workflow_id="w_real_1")
    assert isinstance(block, SendEmailBlock)
    assert [key for key in parameters if key in PLATFORM_SMTP_AWS_KEYS] == []
    assert block.smtp_host.aws_key == UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY
    assert block.smtp_password.aws_key == UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY


def test_nested_send_email_block_provisions_with_the_real_workflow_id() -> None:
    yaml = WhileLoopBlockYAML(
        label="loop",
        condition={"criteria_type": "jinja2_template", "expression": "{{ false }}"},
        loop_blocks=[
            SendEmailBlockYAML(
                label="send_email",
                sender="sender@example.com",
                recipients=["recipient@example.com"],
                subject="subject",
                body="body",
            )
        ],
    )
    parameters = {
        "loop_output": _output_parameter("loop"),
        "send_email_output": _output_parameter("send_email"),
    }
    block_yaml_to_block(yaml, parameters, workflow_id="w_real_1")
    assert {parameters[key].workflow_id for key in PLATFORM_SMTP_AWS_KEYS} == {"w_real_1"}


def test_human_interaction_yaml_needs_only_a_label_and_recipients() -> None:
    yaml = HumanInteractionBlockYAML(label="human_decision", recipients=["approver@example.com"])
    assert yaml.instructions == "Please review and approve or reject to continue the workflow."
    assert yaml.timeout_seconds == 60 * 60 * 2
    assert yaml.sender == "hello@skyvern.com"
    assert yaml.subject == "Human interaction required for workflow run"
    assert yaml.body == "Your interaction is required for a workflow run!"


def test_human_interaction_yaml_still_requires_recipients() -> None:
    with pytest.raises(ValueError):
        HumanInteractionBlockYAML(label="human_decision")


@pytest.mark.asyncio
async def test_send_with_no_recipients_raises_before_transport() -> None:
    with patch("skyvern.forge.sdk.api.email._send") as transport:
        with pytest.raises(ValueError, match="empty"):
            await send(sender="sender@example.com", subject="subject", recipients=[], body="body")
    transport.assert_not_called()


def test_validate_recipients_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_recipients([])


@pytest.mark.parametrize("recipients", [[], ["", " , ; "]])
def test_send_email_block_still_raises_its_own_error_for_no_valid_recipients(recipients: list[str]) -> None:
    block = _send_email_block(recipients=recipients)
    with pytest.raises(NoValidEmailRecipient):
        block.get_real_email_recipients(_run_context())


@pytest.fixture
def offline_address_validation() -> Iterator[None]:
    # email_validator resolves MX records by default; unit tests must not touch DNS.
    with patch("skyvern.forge.sdk.api.email.validate_email", partial(validate_email, check_deliverability=False)):
        yield


def _workflow_run_context(values: dict[str, str]) -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        workflow_run_id="wr_1",
        aws_client=MagicMock(),
    )
    context.values.update(values)
    return context


@pytest.mark.asyncio
async def test_send_delivers_to_every_address_in_a_joined_recipients_entry(offline_address_validation: None) -> None:
    transport = AsyncMock(return_value=True)
    with patch("skyvern.forge.sdk.api.email._send", transport):
        await send(
            sender="sender@example.com",
            subject="subject",
            recipients=["first@example.com, second@example.com; third@example.com ", " "],
            body="body",
        )
    assert transport.call_args.kwargs["message"]["To"] == "first@example.com, second@example.com, third@example.com"


@pytest.mark.asyncio
async def test_human_interaction_block_notifies_every_address_in_a_comma_joined_parameter(
    monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    block = _human_interaction_block(recipients=["{{ notify_to }}"])
    context = _workflow_run_context({"notify_to": "first@example.com, second@example.com, third@example.com"})
    monkeypatch.setattr(HumanInteractionBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    database = _paused_run_database()
    monkeypatch.setattr(app, "DATABASE", database)
    transport = AsyncMock(return_value=True)

    with patch("skyvern.forge.sdk.api.email._send", transport):
        result = await block.execute("wr_1", "wrb_1", organization_id="o_1")

    assert result.success is True, result.failure_reason
    assert transport.call_args.kwargs["message"]["To"] == "first@example.com, second@example.com, third@example.com"
    persisted = database.observer.update_workflow_run_block.call_args_list[0].kwargs
    assert persisted["recipients"] == ["first@example.com", "second@example.com", "third@example.com"]


def test_send_email_block_resolves_a_joined_entry_to_every_address(offline_address_validation: None) -> None:
    block = _send_email_block(recipients=["lead@example.com", "notify_to"])
    context = _run_context(values={"notify_to": "first@example.com; second@example.com"})
    context.has_parameter.side_effect = lambda key: key == "notify_to"

    assert block.get_real_email_recipients(context) == ["lead@example.com", "first@example.com", "second@example.com"]


def test_send_email_block_fails_instead_of_dropping_an_invalid_recipient(offline_address_validation: None) -> None:
    block = _send_email_block(recipients=["lead@example.com, second.approver@example"])
    with pytest.raises(InvalidEmailRecipient) as excinfo:
        block.get_real_email_recipients(_run_context())

    assert (excinfo.value.position, excinfo.value.total) == (2, 2)
    assert "second.approver" not in str(excinfo.value)


def test_undeliverable_recipient_error_never_carries_the_domain_into_a_logged_traceback() -> None:
    domain = "secret-person.example"
    undeliverable = EmailUndeliverableError(f"The domain name {domain} does not exist.")
    recipients = [f"approver@{domain}"]
    with patch("skyvern.forge.sdk.api.email.validate_email", side_effect=undeliverable):
        with pytest.raises(InvalidEmailRecipient) as excinfo:
            validate_recipients(recipients)

    assert str(excinfo.value) == "recipient 1 of 1 has a domain that does not accept email"
    assert domain not in "".join(traceback.format_exception(excinfo.value))
    assert str(pickle.loads(pickle.dumps(excinfo.value))) == str(excinfo.value)


def test_converted_definition_carries_the_provisioned_parameters() -> None:
    definition = convert_workflow_definition(
        workflow_definition_yaml=WorkflowDefinitionYAML(
            parameters=[],
            blocks=[
                SendEmailBlockYAML(
                    label="send_email",
                    sender="sender@example.com",
                    recipients=["recipient@example.com"],
                    subject="subject",
                    body="body",
                )
            ],
        ),
        workflow_id="w_real_1",
    )
    provisioned = {
        parameter.key: parameter for parameter in definition.parameters if isinstance(parameter, AWSSecretParameter)
    }
    assert {key: parameter.aws_key for key, parameter in provisioned.items()} == PLATFORM_SMTP_AWS_KEYS


def test_platform_smtp_parameters_register_only_when_the_context_resolved_them() -> None:
    block = _send_email_block()
    with patch.object(SendEmailBlock, "get_workflow_run_context", return_value=_run_context()):
        assert block.get_all_parameters("wr_1") == []

    resolved = _run_context(secret_values={key: "value" for key in PLATFORM_SMTP_AWS_KEYS})
    with patch.object(SendEmailBlock, "get_workflow_run_context", return_value=resolved):
        registered = block.get_all_parameters("wr_1")
    assert sorted(parameter.key for parameter in registered) == sorted(PLATFORM_SMTP_AWS_KEYS)


def test_unresolvable_platform_smtp_secrets_report_configuration_problems() -> None:
    block = _send_email_block()
    with pytest.raises(InvalidEmailClientConfiguration) as excinfo:
        block._decrypt_smtp_parameters(_run_context(values={}))
    assert "Missing SMTP server" in str(excinfo.value)
    assert "Missing SMTP password" in str(excinfo.value)


def _paused_run_database() -> MagicMock:
    database = MagicMock()
    database.observer.update_workflow_run_block = AsyncMock()
    database.workflow_runs.update_workflow_run = AsyncMock()
    database.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
    # Any status other than paused ends the block's wait loop on its first check.
    database.workflow_runs.get_workflow_run = AsyncMock(return_value=MagicMock(status=WorkflowRunStatus.completed))
    return database


async def _human_interaction_message(
    monkeypatch: pytest.MonkeyPatch, block: HumanInteractionBlock, browser_session_id: str | None = None
) -> EmailMessage:
    monkeypatch.setattr(
        HumanInteractionBlock, "get_workflow_run_context", staticmethod(lambda _run_id: _workflow_run_context({}))
    )
    monkeypatch.setattr(app, "DATABASE", _paused_run_database())
    transport = AsyncMock(return_value=True)
    with patch("skyvern.forge.sdk.api.email._send", transport):
        result = await block.execute("wr_1", "wrb_1", organization_id="o_1", browser_session_id=browser_session_id)
    assert result.success is True, result.failure_reason
    return transport.call_args.kwargs["message"]


def test_body_format_round_trips_from_yaml_for_both_blocks() -> None:
    assert _send_email_block(body_format="html").body_format == EmailBodyFormat.HTML
    human_yaml = HumanInteractionBlockYAML(label="approve", recipients=["approver@example.com"], body_format="html")
    human_block = block_yaml_to_block(human_yaml, {"approve_output": _output_parameter("approve")})
    assert isinstance(human_block, HumanInteractionBlock)
    assert human_block.body_format == EmailBodyFormat.HTML


@pytest.mark.asyncio
async def test_text_body_format_still_sends_a_single_plain_text_part(offline_address_validation: None) -> None:
    block = _send_email_block(body="<b>not html</b>\nline 2")
    message = await block._build_email_message(_run_context(), "wr_1", organization_id="o_1")
    assert message.get_content_type() == "text/plain"
    assert message.get_content() == "<b>not html</b>\nline 2\n"


@pytest.mark.asyncio
async def test_html_body_format_sends_sanitized_html_with_a_generated_text_part(
    offline_address_validation: None,
) -> None:
    block = _send_email_block(
        body_format="html",
        body=(
            "<!DOCTYPE html><html><head><style>h1 {color: red}</style>"
            '<meta charset="utf-8"/><meta http-equiv="Refresh" content="0;url=https://evil.example"/></head><body>'
            '<h1>Weekly report</h1><p>12 invoices for <b>Acme</b>. <a href="https://example.com/run">Open</a></p>'
            '<script>alert(1)</script><a href="javascript:void(0)" onclick="steal()">x</a>'
            '<a href="java\tscript:steal()">tab</a><a href=" JAVASCRIPT:steal()">upper</a>'
            '<a href="java\u200bscript:steal()">zero-width</a><a href="data:text/html,evil">data</a>'
            '<a href="vbscript:steal()">vb</a><a href="mailto:a@example.com">mail</a><a href="/relative">rel</a>'
            '<img src="data:image/png;base64,AAAA"/><img src="data:image/svg+xml,%3Csvg%3E"/>'
            '<a href="data:image/png;base64,AAAA">img-link</a>'
            '<iframe src="https://evil.example"></iframe></body></html>'
        ),
    )
    message = await block._build_email_message(_run_context(), "wr_1", organization_id="o_1")

    assert message.get_content_type() == "multipart/alternative"
    text_part, html_part = message.iter_parts()
    assert (text_part.get_content_type(), html_part.get_content_type()) == ("text/plain", "text/html")
    html_body = html_part.get_content()
    assert html_body.startswith("<!DOCTYPE html>")
    assert "<style>h1 {color: red}</style>" in html_body and "<b>Acme</b>" in html_body
    for forbidden in (
        "<script",
        "<iframe",
        "javascript:",
        "onclick",
        "script:steal",
        "data:text/html",
        "svg+xml",
        "refresh",
    ):
        assert forbidden.lower() not in html_body.lower()
    assert '<meta charset="utf-8"/>' in html_body
    assert html_body.count("data:image/png") == 1
    for kept in (
        'href="https://example.com/run"',
        'href="mailto:a@example.com"',
        'href="/relative"',
        'src="data:image/png;base64,AAAA"',
    ):
        assert kept in html_body
    text_body = text_part.get_content()
    assert text_body.splitlines()[0] == "Weekly report"
    assert "12 invoices for Acme. Open (https://example.com/run)" in text_body
    assert "alert(1)" not in text_body and "color: red" not in text_body


@pytest.mark.asyncio
async def test_html_body_with_an_attachment_nests_the_alternative_inside_mixed(
    tmp_path: Path, offline_address_validation: None
) -> None:
    attachment = tmp_path / "report.bin"
    attachment.write_bytes(b"\x00binary")
    block = _send_email_block(body_format="html", body="<p>See the attached report.</p>")
    with patch.object(SendEmailBlock, "_get_file_paths", return_value=[str(attachment)]):
        message = await block._build_email_message(_run_context(), "wr_1", organization_id="o_1")

    assert message.get_content_type() == "multipart/mixed"
    alternative, attached = message.iter_parts()
    assert alternative.get_content_type() == "multipart/alternative"
    assert attached.get_filename() == "report.bin"
    assert "<p>See the attached report.</p>" in message.get_body(preferencelist=("html",)).get_content()


@pytest.mark.asyncio
async def test_human_interaction_text_footer_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    block = _human_interaction_block(
        body="Please approve",
        instructions="Check the totals",
    )
    message = await _human_interaction_message(monkeypatch, block, browser_session_id="pbs_1")
    assert message.get_content_type() == "text/plain"
    assert message.get_content() == (
        f"Please approve\n\nKindly visit {settings.SKYVERN_APP_URL}/runs/wr_1/overview\n\nCheck the totals\n\n"
        f"To interact with the browser session directly, visit {settings.SKYVERN_APP_URL}/browser-session/pbs_1\n\n"
    )


@pytest.mark.asyncio
async def test_human_interaction_html_footer_lands_inside_the_document(
    monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    block = _human_interaction_block(
        body_format="html",
        body="<html><body><h1>Review needed</h1></body></html>",
        instructions="Check <the> totals\nthen decide",
    )
    message = await _human_interaction_message(monkeypatch, block, browser_session_id="pbs_1")
    assert message.get_content_type() == "multipart/alternative"
    html_body = message.get_body(preferencelist=("html",)).get_content()
    run_url = f"{settings.SKYVERN_APP_URL}/runs/wr_1/overview"
    assert html_body.index("<h1>Review needed</h1>") < html_body.index("Kindly visit") < html_body.index("</body>")
    assert f'<a href="{run_url}">{run_url}</a>' in html_body
    assert "/browser-session/pbs_1" in html_body
    assert "Check &lt;the&gt; totals<br/>then decide" in html_body
    text_body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Review needed" in text_body and run_url in text_body


@pytest.mark.asyncio
async def test_human_interaction_html_footer_appends_to_a_fragment(
    monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    block = _human_interaction_block(
        body_format="html",
        body="<p>Review needed</p>",
    )
    message = await _human_interaction_message(monkeypatch, block)
    html_body = message.get_body(preferencelist=("html",)).get_content()
    assert html_body.startswith("<p>Review needed</p>")
    assert "Kindly visit" in html_body and "browser session" not in html_body


@pytest.mark.asyncio
async def test_human_interaction_html_footer_ignores_a_decoy_body_close_tag(
    monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    block = _human_interaction_block(
        body_format="html",
        body="<html><body><!-- </body> --><h1>Review needed</h1><style>p:after { content: '</body>' }</style></body></html>",
    )
    message = await _human_interaction_message(monkeypatch, block)
    html_body = message.get_body(preferencelist=("html",)).get_content()
    assert html_body.count("Kindly visit") == 1
    assert html_body.index("<style>") < html_body.index("Kindly visit") < html_body.rindex("</body>")


def test_html_body_without_beautifulsoup_fails_with_an_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_api, "BeautifulSoup", None)
    message = EmailMessage()
    with pytest.raises(RuntimeError, match="beautifulsoup4"):
        email_api.set_body(message, "<p>x</p>", EmailBodyFormat.HTML)
    email_api.set_body(message, "plain", EmailBodyFormat.TEXT)
    assert message.get_content() == "plain\n"


@pytest.mark.asyncio
async def test_human_interaction_html_footer_stays_inside_a_document_without_a_body_tag(
    monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    block = _human_interaction_block(body_format="html", body="<html><p>Review needed</p></html>")
    message = await _human_interaction_message(monkeypatch, block)
    html_body = message.get_body(preferencelist=("html",)).get_content()
    assert html_body.index("Kindly visit") < html_body.rindex("</html>")


@pytest.mark.asyncio
async def test_html_body_drops_foreign_content_that_would_re_parse_into_live_markup(
    offline_address_validation: None,
) -> None:
    """html.parser keeps a <style> payload nested in <svg>/<math> as raw text, so the attribute walk
    never sees it and an HTML5 client re-parses it back into a live <img onerror>."""
    block = _send_email_block(
        body_format="html",
        body=(
            "<h1>Report</h1>"
            "<svg><style><img src=x onerror=alert(1)></style></svg>"
            "<math><style><img src=x onerror=alert(2)></style></math>"
            "<table><tr><td><SVG><STYLE><img src=x onerror=alert(3)></STYLE></SVG></td></tr></table>"
        ),
    )
    message = await block._build_email_message(_run_context(), "wr_1", organization_id="o_1")
    html_body = message.get_body(preferencelist=("html",)).get_content()

    for forbidden in ("<svg", "<math", "onerror", "alert("):
        assert forbidden.lower() not in html_body.lower()
    assert "<h1>Report</h1>" in html_body
    assert "<table>" in html_body


def test_generated_script_emits_body_format_only_when_html() -> None:
    # The generator receives model_dump() output, where body_format is the enum member, not a str.
    def compact(block: dict) -> str:
        return cst.Module(body=[_build_send_email_statement(block)]).code.replace(" ", "").replace("\n", "")

    assert "body_format='html'" in compact(_send_email_block(body_format="html").model_dump())
    assert "body_format" not in compact(_send_email_block().model_dump())
    legacy_block = {"label": "notify", "sender": "me@example.com", "recipients": [], "subject": "s", "body": "b"}
    assert "body_format" not in compact(legacy_block)


@pytest.mark.asyncio
async def test_subject_carries_only_what_the_user_wrote(offline_address_validation: None) -> None:
    context = _workflow_run_context({})

    block = _send_email_block(subject="  Your order shipped  ")
    block.format_potential_template_parameters(context)

    assert (await block._build_email_message(context, "wr_1"))["Subject"] == "Your order shipped"


@pytest.mark.asyncio
async def test_subject_renders_the_run_id_where_the_user_placed_it(offline_address_validation: None) -> None:
    context = _workflow_run_context({})

    block = _send_email_block(subject="Your Run is Finished {{workflow_run_id}}")
    block.format_potential_template_parameters(context)

    assert (await block._build_email_message(context, "wr_1"))["Subject"] == "Your Run is Finished wr_1"


@pytest.mark.asyncio
async def test_a_substituted_newline_is_flattened_rather_than_failing_the_send(
    offline_address_validation: None,
) -> None:
    context = _workflow_run_context({"injected": "ok\r\nBcc: attacker@example.com"})

    block = _send_email_block(subject="Report {{injected}}")
    block.format_potential_template_parameters(context)
    message = await block._build_email_message(context, "wr_1")

    assert message["Subject"] == "Report okBcc: attacker@example.com"
    assert message["BCC"] == "sender@example.com"


@pytest.mark.asyncio
async def test_email_download_directory_ignores_previous_attempt_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))
    download_dir = tmp_path / "wr_email"
    download_dir.mkdir()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    for name, offset in (("old.txt", -1), ("fresh.txt", 0)):
        path = download_dir / name
        path.write_text(name)
        os.utime(path, (started_at.timestamp() + offset,) * 2)
    block = _send_email_block(file_attachments=[settings.WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY])
    context = _run_context()
    with (
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch(
            "skyvern.forge.sdk.artifact.storage.base.resolve_download_attempt",
            AsyncMock(return_value=("wr_email", 2, started_at)),
        ),
    ):
        message = await block._build_email_message(context, "wr_email", "o_1")

    assert [part.get_filename() for part in message.iter_attachments()] == ["fresh.txt"]
    assert (download_dir / "old.txt").read_text() == "old.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "  "])
async def test_a_blank_attachment_entry_is_skipped_and_the_email_still_builds(
    blank: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))
    report = tmp_path / "wr_1" / "report.txt"
    report.parent.mkdir()
    report.write_text("report")
    context = _workflow_run_context({})

    with patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None):
        only_blank = await _send_email_block(file_attachments=[blank])._build_email_message(context, "wr_1")
        mixed = await _send_email_block(file_attachments=[blank, str(report), blank])._build_email_message(
            context, "wr_1"
        )

    assert list(only_blank.iter_attachments()) == []
    assert [part.get_filename() for part in mixed.iter_attachments()] == ["report.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["{{ report_path }}", "report_path"])
async def test_an_attachment_that_resolves_to_an_empty_path_still_fails(
    entry: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_address_validation: None
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))
    context = _workflow_run_context({"report_path": ""})
    now = datetime.now(UTC)
    context.parameters["report_path"] = WorkflowParameter(
        key="report_path",
        workflow_parameter_id="wp_1",
        workflow_parameter_type=WorkflowParameterType.STRING,
        workflow_id="w_1",
        created_at=now,
        modified_at=now,
    )

    with patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None):
        with pytest.raises(PermissionError, match="path must not be empty"):
            await _send_email_block(file_attachments=[entry])._build_email_message(context, "wr_1")


GMAIL_ORG = "o_gmail"
GMAIL_RUN = "wr_gmail"
GMAIL_CREDENTIAL = "goac_gmail"
GMAIL_SENDER = "verified.sender@example.com"
SENTINEL_TOKEN = "ya29.sentinel-access-token"
SENTINEL_RECIPIENT = "sentinel.recipient@example.com"
SENTINEL_SUBJECT = "Sentinel subject 7f3a"
SENTINEL_BODY = "Sentinel body 9c1e"
SENTINELS = (SENTINEL_TOKEN, SENTINEL_RECIPIENT, SENTINEL_SUBJECT, SENTINEL_BODY)
GMAIL_TOKEN = google_oauth_service.GoogleRefreshResult(SENTINEL_TOKEN, None)
GmailResponder = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
ACCEPTED_OUTPUT = {
    "success": True,
    "transport": "gmail",
    "outcome": "accepted",
    "provider_message_id": "msg-1",
    "error_code": None,
}


def _accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "msg-1", "threadId": "thread-1"})


def _rejected(request: httpx.Request) -> httpx.Response:
    return httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT", "message": " ".join(SENTINELS)}})


async def _time_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


@dataclass
class GmailEnv:
    database: AgentDB
    token_mints: AsyncMock
    respond: GmailResponder = _accepted
    requests: list[httpx.Request] = field(default_factory=list)

    def sent(self) -> list[EmailMessage]:
        return [message_from_bytes(request.content, policy=email_policy.default) for request in self.requests]

    async def rows(self, model: type[Base]) -> list[Base]:
        async with self.database.Session() as session:
            return list((await session.scalars(select(model))).all())

    async def add_run(
        self, workflow_run_id: str, retried_from: str | None = None, organization_id: str = GMAIL_ORG, **values: object
    ) -> None:
        async with self.database.Session() as session:
            session.add(
                WorkflowRunModel(
                    workflow_run_id=workflow_run_id,
                    workflow_id="w_1",
                    workflow_permanent_id="wpid_1",
                    organization_id=organization_id,
                    status="running",
                    retried_from_workflow_run_id=retried_from,
                )
            )
            await session.commit()
        context = _workflow_run_context(values)
        context.workflow_run_id, context.organization_id = workflow_run_id, GMAIL_ORG
        app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[workflow_run_id] = context

    async def set_connection(self, **values: object) -> None:
        async with self.database.Session() as session:
            await session.execute(
                update(GoogleOAuthCredentialModel)
                .where(GoogleOAuthCredentialModel.id == GMAIL_CREDENTIAL)
                .values(modified_at=GoogleOAuthCredentialModel.modified_at, **values)
            )
            await session.commit()

    def run_during_the_next_token_mint(self, block: SendEmailBlock, workflow_run_id: str = GMAIL_RUN) -> None:
        """Run another execution to completion once the next one has read the send record but not yet claimed it."""
        overtaken_mint = self.token_mints.await_count + 1

        async def mint(**_: object) -> google_oauth_service.GoogleRefreshResult:
            if self.token_mints.await_count == overtaken_mint:
                await _run_gmail(block, workflow_run_id)
            return GMAIL_TOKEN

        self.token_mints.side_effect = mint


@pytest_asyncio.fixture
async def gmail_env(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine, offline_address_validation: None, tmp_path: Path
) -> AsyncIterator[GmailEnv]:
    database = AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    monkeypatch.setattr(app, "SECONDARY_LLM_API_HANDLER", AsyncMock(return_value={"summary": "description"}))
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path / "temp"))
    async with database.Session() as session:
        session.add(
            GoogleOAuthCredentialModel(
                id=GMAIL_CREDENTIAL,
                organization_id=GMAIL_ORG,
                credential_name="Mail",
                state="active",
                scopes_granted=[google_oauth_service.GOOGLE_GMAIL_SEND_SCOPE, "openid"],
                email_address=GMAIL_SENDER,
                google_subject="subject-1",
                encrypted_refresh_token="enc-refresh",
                encrypted_method="aes",
            )
        )
        await session.commit()
    env = GmailEnv(database=database, token_mints=AsyncMock(return_value=GMAIL_TOKEN))
    await env.add_run(GMAIL_RUN)

    async def handle(request: httpx.Request) -> httpx.Response:
        env.requests.append(request)
        response = env.respond(request)
        return await response if isinstance(response, Awaitable) else response

    monkeypatch.setattr(google_oauth_service, "encryptor", SimpleNamespace(decrypt=AsyncMock(return_value="refresh")))
    monkeypatch.setattr(google_oauth_service, "refresh_and_rotate", env.token_mints)
    monkeypatch.setattr(smtplib, "SMTP", MagicMock(side_effect=AssertionError("a Gmail block opened SMTP")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as gmail_client:
        monkeypatch.setattr(
            "skyvern.forge.sdk.workflow.models.block.send_raw_message",
            partial(gmail_service.send_raw_message, client=gmail_client),
        )
        yield env
    skyvern_context.reset()


def _gmail_block(label: str = "send_email", **overrides: object) -> SendEmailBlock:
    fields = {
        "credential_id": GMAIL_CREDENTIAL,
        "recipients": [SENTINEL_RECIPIENT],
        "subject": SENTINEL_SUBJECT,
        "body": SENTINEL_BODY,
        **overrides,
    }
    block = block_yaml_to_block(
        SendEmailBlockYAML(label=label, transport="gmail", **fields), {f"{label}_output": _output_parameter(label)}
    )
    assert isinstance(block, SendEmailBlock)
    return block


async def _run_gmail(block: SendEmailBlock, workflow_run_id: str = GMAIL_RUN) -> BlockResult:
    return await block.execute_safe(workflow_run_id=workflow_run_id, organization_id=GMAIL_ORG)


def _outcome(result: BlockResult) -> tuple[str, str | None, bool]:
    output = result.output_parameter_value
    return output["outcome"], output["error_code"], output["replayed"]


def _attachments(message: EmailMessage) -> list[tuple[str | None, bytes]]:
    return [(part.get_filename(), part.get_content()) for part in message.iter_attachments()]


@pytest.mark.asyncio
async def test_gmail_send_builds_one_message_from_the_verified_account(gmail_env: GmailEnv, tmp_path: Path) -> None:
    run_dir = tmp_path / "downloads" / GMAIL_RUN
    run_dir.mkdir(parents=True)
    report_bytes = bytes(range(256)) * 4
    (run_dir / "report.bin").write_bytes(report_bytes)
    (run_dir / "unrequested.txt").write_text("not asked for")

    result = await _run_gmail(
        _gmail_block(
            sender="someone.else@example.com",
            recipients=["to.one@example.com, to.two@example.com"],
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
            subject="Rapport d'activité ✓\r\nX-Injected: yes",
            body="<p>Résumé — <b>terminé</b></p>",
            body_format="html",
            file_attachments=[str(run_dir / "report.bin")],
        )
    )

    (request,) = gmail_env.requests
    (message,) = gmail_env.sent()
    assert (request.url.params["uploadType"], request.headers["content-type"]) == ("media", "message/rfc822")
    assert request.headers["authorization"] == f"Bearer {SENTINEL_TOKEN}"
    assert (message["From"], message["To"]) == (GMAIL_SENDER, "to.one@example.com, to.two@example.com")
    assert (message["Cc"], message["Bcc"], message["X-Injected"]) == ("cc@example.com", "bcc@example.com", None)
    assert message["Subject"] == "Rapport d'activité ✓X-Injected: yes"
    assert "Résumé — <b>terminé</b>" in message.get_body(preferencelist=("html",)).get_content()
    assert _attachments(message) == [("report.bin", report_bytes)]
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value == {**ACCEPTED_OUTPUT, "replayed": False}
    (dispatch,) = await gmail_env.rows(GmailSendDispatchModel)
    assert (dispatch.status, dispatch.provider_message_id) == ("accepted", "msg-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "connection", "expected"),
    [
        pytest.param({"credential_id": None}, {}, "configuration", id="no-connection"),
        pytest.param({"credential_id": "goac_missing"}, {}, "configuration", id="stale-connection-id"),
        pytest.param({}, {"organization_id": "o_other"}, "configuration", id="another-organizations-connection"),
        pytest.param(
            {},
            {"scopes_granted": [google_oauth_service.GOOGLE_GMAIL_READONLY_SCOPE]},
            "missing_scope",
            id="read-only-connection",
        ),
        pytest.param({}, {"state": "revoked"}, "configuration", id="revoked"),
        pytest.param({}, {"google_subject": None}, "reconnect", id="no-verified-identity"),
        pytest.param({"recipients": [], "cc": [""]}, {}, "no_recipients", id="no-recipients"),
        pytest.param(
            {"recipients": ["to@example.com\r\nBcc: injected@example.com"]},
            {},
            "invalid_recipient",
            id="injected-recipient",
        ),
        pytest.param(
            {"file_attachments": ["SKYVERN_DOWNLOAD_DIRECTORY"]}, {}, "attachment_invalid", id="download-directory"
        ),
    ],
)
async def test_gmail_send_refusals_make_no_token_or_provider_call(
    gmail_env: GmailEnv, overrides: dict[str, object], connection: dict[str, object], expected: str
) -> None:
    if connection:
        await gmail_env.set_connection(**connection)

    result = await _run_gmail(_gmail_block(**overrides))

    assert result.status == BlockStatus.failed
    assert _outcome(result) == ("failed", expected, False)
    assert gmail_env.requests == []
    gmail_env.token_mints.assert_not_awaited()
    assert await gmail_env.rows(GmailSendDispatchModel) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "respond",
    [
        pytest.param(_time_out, id="timeout"),
        pytest.param(lambda _: httpx.Response(200, json={"threadId": "t"}), id="no-message-id"),
        pytest.param(lambda _: httpx.Response(503, text="unavailable"), id="503"),
        pytest.param(lambda _: httpx.Response(422, json={"error": {}}), id="unlisted-4xx"),
    ],
)
async def test_gmail_send_unconfirmed_attempt_is_unknown_and_never_sent_again(
    gmail_env: GmailEnv, respond: GmailResponder
) -> None:
    gmail_env.respond = respond

    first = await _run_gmail(_gmail_block())
    gmail_env.respond = _accepted
    replay = await _run_gmail(_gmail_block())

    assert len(gmail_env.requests) == 1
    assert first.status == replay.status == BlockStatus.failed
    assert _outcome(first) == ("unknown", "outcome_unknown", False)
    assert _outcome(replay) == ("unknown", "outcome_unknown", True)
    assert [row.status for row in await gmail_env.rows(GmailSendDispatchModel)] == ["unknown"]


@pytest.mark.asyncio
async def test_gmail_send_interrupted_dispatch_is_unknown_and_not_sent_again(gmail_env: GmailEnv) -> None:
    reached_gmail = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        reached_gmail.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    gmail_env.respond = hang
    interrupted = asyncio.create_task(_run_gmail(_gmail_block()))
    await asyncio.wait_for(reached_gmail.wait(), timeout=10)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(interrupted, timeout=10)

    gmail_env.respond = _accepted
    recovery = await _run_gmail(_gmail_block())

    assert len(gmail_env.requests) == 1
    assert recovery.status == BlockStatus.failed
    assert _outcome(recovery) == ("unknown", "outcome_unknown", True)
    assert [row.status for row in await gmail_env.rows(GmailSendDispatchModel)] == ["dispatching"]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_a_rejected_attempt", [False, True], ids=["first-attempt", "after-a-rejected-attempt"])
async def test_gmail_send_concurrent_duplicate_execution_dispatches_once_and_reuses_the_receipt(
    gmail_env: GmailEnv, after_a_rejected_attempt: bool
) -> None:
    if after_a_rejected_attempt:
        gmail_env.respond = _rejected
        await _run_gmail(_gmail_block())
        gmail_env.respond = _accepted
    earlier_requests = len(gmail_env.requests)

    gmail_env.run_during_the_next_token_mint(_gmail_block())
    overtaken = await _run_gmail(_gmail_block())

    assert len(gmail_env.requests) == earlier_requests + 1
    assert overtaken.output_parameter_value == {**ACCEPTED_OUTPUT, "replayed": True}
    assert [row.status for row in await gmail_env.rows(GmailSendDispatchModel)] == ["accepted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("respond", "retry_requests", "retry_outcome"),
    [
        pytest.param(_accepted, 0, "accepted", id="accepted-is-replayed"),
        pytest.param(_time_out, 0, "unknown", id="unknown-is-not-sent-again"),
        pytest.param(_rejected, 1, "accepted", id="rejected-is-attempted-once-more"),
    ],
)
async def test_gmail_send_credential_fallback_retry_follows_the_outcome_the_original_run_recorded(
    gmail_env: GmailEnv, respond: GmailResponder, retry_requests: int, retry_outcome: str
) -> None:
    await gmail_env.add_run("wr_retry", retried_from=GMAIL_RUN)
    await gmail_env.add_run("wr_second_retry", retried_from="wr_retry")
    await gmail_env.add_run("wr_unrelated")
    gmail_env.respond = respond

    await _run_gmail(_gmail_block())
    gmail_env.respond = _accepted
    retry = await _run_gmail(_gmail_block(), "wr_retry")
    second_retry = await _run_gmail(_gmail_block(), "wr_second_retry")
    retry_chain_requests = len(gmail_env.requests)
    unrelated = await _run_gmail(_gmail_block(), "wr_unrelated")

    assert retry_chain_requests == 1 + retry_requests
    assert (_outcome(retry)[0], _outcome(retry)[2]) == (retry_outcome, retry_requests == 0)
    assert (_outcome(second_retry)[0], _outcome(second_retry)[2]) == (retry_outcome, True)
    assert _outcome(unrelated) == ("accepted", None, False)


@pytest.mark.asyncio
async def test_gmail_send_does_not_follow_a_retry_link_into_another_organization(gmail_env: GmailEnv) -> None:
    await gmail_env.add_run("wr_other_org", organization_id="o_other")
    await gmail_env.add_run("wr_retry", retried_from="wr_other_org")

    result = await _run_gmail(_gmail_block(), "wr_retry")

    assert result.output_parameter_value["outcome"] == "unknown"
    assert gmail_env.requests == []
    assert await gmail_env.rows(GmailSendDispatchModel) == []


@pytest.mark.asyncio
async def test_gmail_send_keeps_tokens_recipients_and_message_text_out_of_logs_and_stored_rows(
    gmail_env: GmailEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="skyvern")
    await gmail_env.add_run("wr_rejected")
    await gmail_env.add_run("wr_template", note=SENTINEL_BODY)
    await gmail_env.add_run("wr_unknown")

    accepted = await _run_gmail(_gmail_block())
    gmail_env.respond = _rejected
    rejected = await _run_gmail(_gmail_block(), "wr_rejected")
    malformed = await _run_gmail(
        _gmail_block(subject=f"{SENTINEL_SUBJECT} {{{{ note | no_such_filter }}}}", body="{{ note + 1 }}"),
        "wr_template",
    )
    gmail_env.respond = _time_out
    unknown = await _run_gmail(_gmail_block(), "wr_unknown")

    outcomes = [accepted, rejected, malformed, unknown]
    assert [_outcome(outcome)[1] for outcome in outcomes] == [
        None,
        "provider_rejected",
        "template_error",
        "outcome_unknown",
    ]
    rows = [
        row
        for model in (WorkflowRunBlockModel, GmailSendDispatchModel, WorkflowRunOutputParameterModel)
        for row in await gmail_env.rows(model)
    ]
    stored = repr([[getattr(row, column.name) for column in row.__table__.columns] for row in rows])
    for sentinel in SENTINELS:
        assert sentinel not in caplog.text + stored + repr(outcomes)
    assert "INVALID_ARGUMENT" in stored


@pytest.mark.asyncio
async def test_gmail_send_attaches_its_own_copy_of_each_same_named_stored_file_and_leaves_none_on_disk(
    gmail_env: GmailEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await gmail_env.add_run("wr_other")
    files = {
        f"s3://uploads/o_gmail/{folder}/report.pdf": folder.encode() for folder in ("january", "february", "other")
    }
    january, february, other = files
    storage = SimpleNamespace(
        assert_managed_file_access=MagicMock(),
        managed_file_size=AsyncMock(side_effect=lambda uri, _: len(files[uri])),
        download_managed_file=AsyncMock(side_effect=lambda uri, _: files[uri]),
    )
    monkeypatch.setattr(app, "STORAGE", storage)

    gmail_env.run_during_the_next_token_mint(_gmail_block(subject="other", file_attachments=[other]), "wr_other")
    await _run_gmail(_gmail_block(subject="first", file_attachments=[january, february]))

    assert {message["Subject"]: _attachments(message) for message in gmail_env.sent()} == {
        "other": [("report.pdf", b"other")],
        "first": [("report.pdf", b"january"), ("report.pdf", b"february")],
    }
    assert [path for path in (tmp_path / "temp").rglob("*") if not path.is_dir()] == []


def _loop_of(label: str, *children: ForLoopBlock | SendEmailBlock, values: str = "{{ items }}") -> ForLoopBlock:
    return ForLoopBlock(
        label=label,
        output_parameter=_output_parameter(label),
        loop_variable_reference=values,
        loop_blocks=list(children),
    )


async def _cached_run(gmail_env: GmailEnv, workflow_run_id: str, *blocks: ForLoopBlock, **values: object) -> None:
    await gmail_env.add_run(workflow_run_id, **values)
    async with gmail_env.database.Session() as session:
        await session.merge(
            WorkflowModel(
                workflow_id="w_1",
                workflow_permanent_id="wpid_1",
                organization_id=GMAIL_ORG,
                title="Workflow",
                version=1,
                workflow_definition=WorkflowDefinition(parameters=[], blocks=list(blocks)).model_dump(mode="json"),
            )
        )
        await session.commit()
    skyvern_context.set(
        skyvern_context.SkyvernContext(
            organization_id=GMAIL_ORG, workflow_id="w_1", workflow_run_id=workflow_run_id, script_mode=True
        )
    )


@pytest.mark.asyncio
async def test_gmail_send_cached_loops_send_once_per_iteration_and_an_engine_rerun_sends_nothing(
    gmail_env: GmailEnv,
) -> None:
    outer = _loop_of(
        "outer",
        _loop_of("inner", _gmail_block("inner_send", subject="{{ current_value }}"), values="{{ inner_items }}"),
        _gmail_block(subject="{{ current_value }}"),
    )
    await _cached_run(gmail_env, "wr_loops", outer, items=["one", "two"], inner_items=["x", "y"])
    # The engine nests a block under its conditional's row and cached code does not; both must name one execution.
    conditional_row = await gmail_env.database.observer.create_workflow_run_block(
        workflow_run_id="wr_loops", organization_id=GMAIL_ORG, block_type=BlockType.CONDITIONAL, label="branch"
    )

    async for _ in script_service.loop(["one", "two"], label="outer"):
        async for _ in script_service.loop(["x", "y"], label="inner"):
            await script_service.send_email(transport="gmail", credential_id=GMAIL_CREDENTIAL, label="inner_send")
        await script_service.send_email(transport="gmail", credential_id=GMAIL_CREDENTIAL, label="send_email")
    skyvern_context.reset()
    rerun = await outer.execute_safe(
        workflow_run_id="wr_loops",
        organization_id=GMAIL_ORG,
        parent_workflow_run_block_id=conditional_row.workflow_run_block_id,
    )

    assert [message["Subject"] for message in gmail_env.sent()] == ["x", "y", "one", "x", "y", "two"]
    assert rerun.status == BlockStatus.completed
    dispatches = await gmail_env.rows(GmailSendDispatchModel)
    assert len({dispatch.execution_key for dispatch in dispatches}) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("inner_items", [["x", "y"], []], ids=["inner-loop-with-values", "empty-inner-loop"])
async def test_cached_nested_loop_records_the_inner_loop_in_each_outer_iteration(
    gmail_env: GmailEnv, inner_items: list[str]
) -> None:
    outer = _loop_of("outer", _loop_of("inner", values="{{ inner_items }}"))
    await _cached_run(gmail_env, "wr_plain_nested", outer, items=["one", "two"], inner_items=inner_items)

    async for _ in script_service.loop(["one", "two"], label="outer"):
        async for item in script_service.loop("{{ inner_items }}", complete_if_empty=True, label="inner"):
            script_service._append_to_loop_output({"row": item.current_value}, "step")

    (outer_row,) = [row for row in await gmail_env.rows(WorkflowRunBlockModel) if row.label == "outer"]
    assert [
        [(entry["output_parameter"]["key"], entry["loop_value"], len(entry["output_value"])) for entry in iteration]
        for iteration in outer_row.output
    ] == [[("inner_output", "one", len(inner_items))], [("inner_output", "two", len(inner_items))]]


def test_gmail_send_block_carries_the_same_fields_through_yaml_conversion_reload_and_codegen() -> None:
    authored = {
        "transport": "gmail",
        "credential_id": GMAIL_CREDENTIAL,
        "recipients": [SENTINEL_RECIPIENT],
        "cc": ["cc@example.com"],
        "bcc": ["bcc@example.com"],
        "subject": SENTINEL_SUBJECT,
        "body": SENTINEL_BODY,
        "body_format": "html",
        "file_attachments": ["report.pdf"],
    }

    block = block_yaml_to_block(_send_email_block_yaml(sender="", **authored), _default_parameters())
    stored = block.model_dump(mode="json")
    reloaded = SendEmailBlock.model_validate(stored)
    call = ast.parse(cst.Module(body=[_build_send_email_statement(stored)]).code).body[0].value.value

    assert {name: stored[name] for name in authored} == authored
    assert reloaded.model_dump(mode="json") == stored
    assert {reloaded.smtp_host.aws_key, reloaded.smtp_password.aws_key} == {UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY}
    assert call.args == []
    assert {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords} == {
        "transport": "gmail",
        "credential_id": GMAIL_CREDENTIAL,
        "label": "send_email",
    }


def test_gmail_send_workflow_saves_the_platform_smtp_parameters_only_while_an_smtp_block_uses_them() -> None:
    gmail = SendEmailBlockYAML(label="gmail", transport="gmail", recipients=[], subject="", body="")
    looped_gmail = ForLoopBlockYAML(label="each", loop_variable_reference="{{ items }}", loop_blocks=[gmail])
    declared = [
        *(AWSSecretParameterYAML(key=key, aws_key=aws_key) for key, aws_key in PLATFORM_SMTP_AWS_KEYS.items()),
        AWSSecretParameterYAML(key="api_key", aws_key="UNRELATED_SECRET"),
    ]

    def convert(*blocks: SendEmailBlockYAML | ForLoopBlockYAML) -> tuple[dict[str, str], list]:
        definition = convert_workflow_definition(
            WorkflowDefinitionYAML(parameters=declared, blocks=list(blocks)), workflow_id="w_1"
        )
        secrets = {p.key: p.aws_key for p in definition.parameters if isinstance(p, AWSSecretParameter)}
        return secrets, definition.blocks

    gmail_only, _ = convert(looped_gmail)
    with_smtp, (_, gmail_block) = convert(_send_email_block_yaml(label="smtp"), gmail)
    placeholders = (gmail_block.smtp_host, gmail_block.smtp_port, gmail_block.smtp_username, gmail_block.smtp_password)

    assert gmail_only == {"api_key": "UNRELATED_SECRET"}
    assert with_smtp == {**PLATFORM_SMTP_AWS_KEYS, "api_key": "UNRELATED_SECRET"}
    # A rollback must not find a Gmail block keyed to the workflow's real SMTP secrets.
    assert len({placeholder.key for placeholder in placeholders}) == 4
    assert {placeholder.key for placeholder in placeholders}.isdisjoint(with_smtp)


def test_gmail_send_yaml_keeps_a_blank_draft_and_rejects_the_other_transports_fields() -> None:
    draft = SendEmailBlockYAML(label="send_email", transport="gmail", recipients=[], subject="", body="")

    assert (draft.sender, draft.credential_id, draft.recipients, draft.cc, draft.bcc) == ("", None, [], [], [])
    assert _send_email_block_yaml().transport is None
    with pytest.raises(ValidationError, match="sender is required unless the transport is gmail"):
        SendEmailBlockYAML(label="send_email", recipients=["to@example.com"], subject="s", body="b")
    with pytest.raises(ValidationError, match="cannot be combined with the gmail transport"):
        _send_email_block_yaml(transport="gmail", custom_smtp_host="smtp.example.com")
