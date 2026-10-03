import asyncio
import re
import smtplib
import unicodedata
from email.message import EmailMessage

import structlog
from email_validator import EmailNotValidError, EmailSyntaxError, EmailUndeliverableError, validate_email

from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.schemas.emails import EmailBodyFormat

try:
    from bs4 import BeautifulSoup
    from bs4.element import NavigableString, PreformattedString, Tag
except ImportError:  # pragma: no cover - beautifulsoup4 ships with the server extra only
    BeautifulSoup = NavigableString = PreformattedString = Tag = None  # type: ignore[assignment, misc]

LOG = structlog.get_logger()

# Per-op socket timeout so the executor thread cannot linger past an outer asyncio cancel.
_SMTP_SOCKET_TIMEOUT_SECONDS = 10

# The workflow editor only splits a comma-separated Recipients field client-side, so a workflow
# parameter substituted into that field arrives at runtime as one comma- or semicolon-joined entry.
_RECIPIENT_SEPARATORS = re.compile(r"[,;]")

# svg/math are foreign content: html.parser keeps a nested <style>/<script> payload as raw text, so the
# attribute walk below never sees it, and an HTML5 client re-parses it back into live markup.
_UNSAFE_HTML_TAGS = ("script", "iframe", "object", "embed", "svg", "math")
_URL_ATTRIBUTES = frozenset({"href", "src", "action", "formaction", "xlink:href"})
_NON_TEXT_HTML_TAGS = ("head", "title", "style", "script")
# Parsers ignore control, format and whitespace characters inside a URL scheme, so drop them before matching.
_IGNORED_URL_CHAR_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})
_URL_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
_SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto", "tel", "cid"})
_INERT_INLINE_IMAGE_PREFIXES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp")
_INLINE_WHITESPACE = re.compile(r"[ \t]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")
_LINE_BREAK_HTML_TAGS = (
    "p",
    "div",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "ul",
    "ol",
    "blockquote",
    "pre",
)


def _is_unsafe_url(attribute: str, value: str) -> bool:
    cleaned = "".join(char for char in value if unicodedata.category(char) not in _IGNORED_URL_CHAR_CATEGORIES)
    match = _URL_SCHEME.match(cleaned)
    if match is None:
        return False
    scheme = match.group(1).lower()
    if scheme == "data":
        # Only raster images embedded in <img src>; SVG data URLs can carry scripts.
        return attribute != "src" or not cleaned[match.end() :].lower().startswith(_INERT_INLINE_IMAGE_PREFIXES)
    return scheme not in _SAFE_URL_SCHEMES


def _remove_active_content(soup: BeautifulSoup) -> None:
    # Mail clients never run scripts; stripping them protects the shared sender's reputation
    # while leaving author styling (<style>, tables, inline CSS, a full document) untouched.
    for element in soup.find_all(_UNSAFE_HTML_TAGS):
        element.decompose()
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv", "")).strip().lower() == "refresh":
            meta.decompose()
    for element in soup.find_all(True):
        for attribute in list(element.attrs):
            value = element.attrs[attribute]
            is_event_handler = attribute.startswith("on")
            is_unsafe_url = attribute in _URL_ATTRIBUTES and isinstance(value, str) and _is_unsafe_url(attribute, value)
            if is_event_handler or is_unsafe_url:
                del element.attrs[attribute]


def _plain_text(soup: BeautifulSoup) -> str:
    chunks: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, PreformattedString):
                continue
            if isinstance(child, NavigableString):
                chunks.append(str(child))
                continue
            if not isinstance(child, Tag) or child.name in _NON_TEXT_HTML_TAGS:
                continue
            if child.name == "br":
                chunks.append("\n")
                continue
            is_block = child.name in _LINE_BREAK_HTML_TAGS
            if is_block:
                chunks.append("\n")
            walk(child)
            href = child.get("href") if child.name == "a" else None
            if (
                isinstance(href, str)
                and href.startswith(("http://", "https://"))
                and child.get_text(strip=True) != href
            ):
                chunks.append(f" ({href})")
            if child.name in ("td", "th"):
                chunks.append(" ")
            if is_block:
                chunks.append("\n")

    walk(soup)
    lines = [_INLINE_WHITESPACE.sub(" ", line).strip() for line in "".join(chunks).splitlines()]
    return _EXCESS_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip() + "\n"


def set_body(
    message: EmailMessage, body: str | None, body_format: EmailBodyFormat, html_footer: str | None = None
) -> None:
    if body_format != EmailBodyFormat.HTML:
        message.set_content(body)
        return
    if BeautifulSoup is None:
        raise RuntimeError(
            "HTML email bodies need the beautifulsoup4 package; install skyvern[local] or skyvern[server]"
        )
    soup = BeautifulSoup(body or "", "html.parser")
    _remove_active_content(soup)
    if html_footer:
        # Append to the real <body> element; a regex on the raw string would match a "</body>" inside a comment or script.
        container = soup.body or soup.html or soup
        for element in list(BeautifulSoup(html_footer, "html.parser").contents):
            container.append(element)
    message.set_content(_plain_text(soup))
    message.add_alternative(str(soup), subtype="html")


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
    body_format: EmailBodyFormat = EmailBodyFormat.TEXT,
    html_footer: str | None = None,
    headers: dict[str, str] | None = None,
) -> EmailMessage:
    to = ", ".join(recipients)
    msg = EmailMessage()
    msg["BCC"] = sender  # BCC the sender so there is a record of the email being sent
    msg["From"] = sender
    msg["Subject"] = subject
    msg["To"] = to
    for name, value in (headers or {}).items():
        msg[name] = value
    set_body(msg, body, body_format, html_footer)

    return msg


async def send(
    *,
    sender: str,
    subject: str,
    recipients: list[str],
    body: str | None = None,
    body_format: EmailBodyFormat = EmailBodyFormat.TEXT,
    html_footer: str | None = None,
    headers: dict[str, str] | None = None,
) -> bool:
    recipients = normalize_recipients(recipients)
    validate_recipients(recipients)

    message = await build_message(
        body=body,
        recipients=recipients,
        sender=sender,
        subject=subject,
        body_format=body_format,
        html_footer=html_footer,
        headers=headers,
    )

    return await _send(message=message)
