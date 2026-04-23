import asyncio
import smtplib
from email.message import EmailMessage

import structlog
from email_validator import EmailNotValidError, validate_email

from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()

# Per-op socket timeout so the executor thread cannot linger past an outer asyncio cancel.
_SMTP_SOCKET_TIMEOUT_SECONDS = 10


def _send_blocking(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    message: EmailMessage,
) -> None:
    smtp_host = smtplib.SMTP(host, port, timeout=_SMTP_SOCKET_TIMEOUT_SECONDS)
    try:
        smtp_host.starttls()
        smtp_host.login(username, password)
        smtp_host.send_message(message)
    finally:
        try:
            smtp_host.quit()
        except smtplib.SMTPException:
            # Connection may already be torn down; fall back to close() and move on.
            smtp_host.close()


async def _send(*, message: EmailMessage) -> bool:
    settings = SettingsManager.get_settings()
    try:
        # smtplib is blocking; offload so the event loop stays free during TLS+AUTH+DATA.
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _send_blocking(
                host=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME,
                password=settings.SMTP_PASSWORD,
                message=message,
            ),
        )
        LOG.info("email: Email sent")
    except Exception as e:
        # Log error_type only. SMTP rejection messages often embed the recipient
        # address (e.g. "550 5.1.1 Recipient rejected: <addr>"), which would leak
        # PII into log aggregators. The exception class plus host/port is enough
        # to triage; callers that need detail can add context around the raise.
        LOG.error(
            "email: Failed to send email",
            error_type=type(e).__name__,
            host=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
        )
        raise

    return True


def validate_recipients(recipients: list[str]) -> None:
    for recipient in recipients:
        try:
            validate_email(recipient)
        except EmailNotValidError as ex:
            # Do not echo the address; callers log downstream and we avoid PII leakage.
            raise ValueError("invalid email address") from ex


async def build_message(
    *,
    body: str | None = None,
    recipients: list[str],
    sender: str,
    subject: str,
) -> EmailMessage:
    to = ", ".join(recipients)
    msg = EmailMessage()
    msg["BCC"] = sender  # BCC the sender so there is a record of the email being sent
    msg["From"] = sender
    msg["Subject"] = subject
    msg["To"] = to
    msg.set_content(body)

    return msg


async def send(
    *,
    sender: str,
    subject: str,
    recipients: list[str],
    body: str | None = None,
) -> bool:
    validate_recipients(recipients)

    message = await build_message(
        body=body,
        recipients=recipients,
        sender=sender,
        subject=subject,
    )

    return await _send(message=message)
