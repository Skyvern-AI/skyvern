import smtplib
from email.message import EmailMessage

import structlog
from email_validator import EmailNotValidError, validate_email

from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


async def _send(*, message: EmailMessage) -> bool:
    settings = SettingsManager.get_settings()
    try:
        smtp_host = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT)

        LOG.info("email: Connected to SMTP server")

        smtp_host.starttls()
        smtp_host.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)

        LOG.info("email: Logged in to SMTP server")

        smtp_host.send_message(message)

        LOG.info("email: Email sent")
    except Exception as e:
        LOG.error("email: Failed to send email", error=str(e), host=settings.SMTP_HOST, port=settings.SMTP_PORT)
        raise e

    return True


def validate_recipients(recipients: list[str]) -> None:
    for recipient in recipients:
        try:
            validate_email(recipient)
        except EmailNotValidError:
            raise Exception(
                f"invalid email address: {recipient}",
            )


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
