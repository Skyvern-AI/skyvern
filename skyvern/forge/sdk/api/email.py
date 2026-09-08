import asyncio
import re
import smtplib
from email.message import EmailMessage

import structlog
from email_validator import EmailNotValidError, EmailSyntaxError, EmailUndeliverableError, validate_email

from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()

# Per-op socket timeout so the executor thread cannot linger past an outer asyncio cancel.
_SMTP_SOCKET_TIMEOUT_SECONDS = 10

# The workflow editor only splits a comma-separated Recipients field client-side, so a workflow
# parameter substituted into that field arrives at runtime as one comma- or semicolon-joined entry.
_RECIPIENT_SEPARATORS = re.compile(r"[,;]")


class InvalidEmailRecipient(ValueError):
    def __init__(self, position: int, total: int, reason: str) -> None:
        super().__init__(position, total, reason)
        self.position = position
        self.total = total
        self.reason = reason

    def __str__(self) -> str:
        return f"recipient {self.position} of {self.total} {self.reason}"


def _rejection_reason(ex: EmailNotValidError) -> str:
    # The validator's own message embeds the domain or fragments of the address, so only the
    # failure class reaches failure_reason and logs.
    if isinstance(ex, EmailUndeliverableError):
        return "has a domain that does not accept email"
    if isinstance(ex, EmailSyntaxError):
        return "is not a well-formed email address"
    return "is not a valid email address"


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


def normalize_recipients(recipients: list[str]) -> list[str]:
    return [
        address
        for entry in recipients
        for address in (part.strip() for part in _RECIPIENT_SEPARATORS.split(entry))
        if address
    ]


def validate_recipients(recipients: list[str]) -> None:
    if not recipients:
        raise ValueError("recipient list cannot be empty")
    for position, recipient in enumerate(recipients, start=1):
        try:
            validate_email(recipient)
        except EmailNotValidError as ex:
            raise InvalidEmailRecipient(position, len(recipients), _rejection_reason(ex)) from None


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
    recipients = normalize_recipients(recipients)
    validate_recipients(recipients)

    message = await build_message(
        body=body,
        recipients=recipients,
        sender=sender,
        subject=subject,
    )

    return await _send(message=message)
