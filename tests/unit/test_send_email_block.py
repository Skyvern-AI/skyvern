"""SEND_EMAIL block tests.

- SKY-12062: a send_email block that references an smtp_* secret parameter which was never
  declared in the workflow's parameters must surface as a handled validation error (422),
  not a bare KeyError (500).
- SKY-14062: optional custom SMTP settings (custom_smtp_*) route the block through the
  user's SMTP server; when absent the default platform sender path is unchanged, and the
  custom password is never echoed in error messages.
"""

from __future__ import annotations

import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import libcst as cst
import pytest

from skyvern.core.script_generations.generate_script import _build_send_email_statement
from skyvern.exceptions import BlockedHost, UnresolvableHost
from skyvern.forge.sdk.api.email import send, validate_recipients
from skyvern.forge.sdk.workflow.exceptions import (
    CustomSMTPAuthenticationFailed,
    CustomSMTPConnectionFailed,
    InvalidEmailClientConfiguration,
    InvalidWorkflowDefinition,
    NoValidEmailRecipient,
)
from skyvern.forge.sdk.workflow.models.block import SendEmailBlock, _send_via_custom_smtp
from skyvern.forge.sdk.workflow.models.parameter import (
    PLATFORM_SMTP_AWS_KEYS,
    UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY,
    AWSSecretParameter,
    OutputParameter,
    ParameterType,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block, convert_workflow_definition
from skyvern.schemas.workflows import (
    HumanInteractionBlockYAML,
    SendEmailBlockYAML,
    WhileLoopBlockYAML,
    WorkflowDefinitionYAML,
)


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
        side_effect=lambda value, _context: f"formatted:{value}",
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
    assert message == (
        "Could not connect to SMTP server bad.example.com:587 "
        "(OSError: [Errno 8] nodename nor servname provided). "
        "Check the SMTP host and port in the Send Email block's Advanced settings."
    )
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


def test_send_email_block_still_raises_its_own_error_for_no_valid_recipients() -> None:
    block = _send_email_block(recipients=[])
    with pytest.raises(NoValidEmailRecipient):
        block.get_real_email_recipients(_run_context())


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
